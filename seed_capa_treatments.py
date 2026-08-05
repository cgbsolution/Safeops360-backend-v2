"""Enrich the 3 ERM-treatment CAPAs on risk ERM-2026-0027 with full data.

These CAPAs are auto-spawned from ERM risk treatments (capa_spawn.spawn_capa),
so they land with only a skeleton — every deep tab (RCA, Actions, Execution,
Verification, Cost, Linkages) renders blank. This backfills realistic, internally
consistent data for demo/review.

SCOPED + IDEMPOTENT:
  • Touches ONLY CAPA-RTM-2026-NW-010 / -011 / -012 (by capaNumber).
  • Re-runnable: deletes the children it manages for these 3 CAPAs, then recreates.
  • Never changes state, owners, severity, priority, closureTargetDate, source refs
    or capaNumber — so the ERM Treatments tab / rollup are unaffected.

    python seed_capa_treatments.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal
from app.models.capa import (
    Capa,
    CapaAction,
    CapaAttachment,
    CapaComment,
    CapaContributor,
    CapaRootCause,
    CapaVerificationMethod,
)
from app.models.user import User

NUMBERS = ["CAPA-RTM-2026-NW-010", "CAPA-RTM-2026-NW-011", "CAPA-RTM-2026-NW-012"]

# children this seed owns (wiped + recreated per run, scoped to the 3 CAPAs)
MANAGED_CHILDREN = (CapaAction, CapaRootCause, CapaContributor, CapaComment, CapaAttachment)


def days(anchor: datetime, n: int) -> datetime:
    return anchor + timedelta(days=n)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        # ── resolve the people involved ──────────────────────────────
        async def uid(name: str) -> str | None:
            u = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
            return u.id if u else None

        MEERA = await uid("Meera Iyer")        # Risk Owner
        PRIYA = await uid("Priya Deshmukh")    # Risk Champion
        RAJESH = await uid("Rajesh Nair")      # Procurement / owner of -011
        ANAND = await uid("Anand Krishnan")    # CRO, raiser/approver

        # ── verification methods by code → id ────────────────────────
        vm = {m.code: m.id for m in (await db.execute(select(CapaVerificationMethod))).scalars().all()}

        # ── load the 3 CAPAs ─────────────────────────────────────────
        capas = {c.capaNumber: c for c in (await db.execute(select(Capa).where(Capa.capaNumber.in_(NUMBERS)))).scalars().all()}
        ids = {n: capas[n].id for n in NUMBERS if n in capas}

        def others(n: str) -> list[str]:
            return [ids[x] for x in NUMBERS if x != n and x in ids]

        # =============================================================
        # Enrichment spec per CAPA. Dates are anchored on each CAPA's own
        # detectedAt so they stay internally consistent.
        # =============================================================
        def spec_010(c: Capa) -> dict:
            d0 = c.detectedAt
            return dict(
                problemImpact=(
                    "A single-source elastane dependency exposes the NW knitting lines to a total "
                    "production stop within 5–7 days of any supplier disruption. Estimated exposure is "
                    "~₹2.1 Cr per month of lost output plus liquidated-damages risk on downstream garment "
                    "export orders."
                ),
                affectedAreas=["NW Knitting Lines 3–7", "Raw Material Store"],
                affectedDepartments=["Manufacturing & Supply Chain", "Procurement", "Quality"],
                affectedProcesses=["Circular knitting", "Yarn sourcing & qualification"],
                rcaMethodology="5_WHY",
                rcaMethodologyRationale="Linear cause chain from a stock-out risk to a governance gap; 5-Why is sufficient.",
                rcaSummary=(
                    "Why can a single supplier halt NW output? No qualified alternate exists. Why? No "
                    "second-source qualification programme was ever run for critical elastane. Why? Procurement "
                    "policy did not mandate dual-sourcing for single-source critical inputs. Why? Supplier "
                    "concentration was never surfaced on the vendor scorecard. Root cause: absence of a governed "
                    "dual-sourcing requirement and concentration monitoring for critical yarns."
                ),
                contributingFactors=[
                    "Long overseas lead time (21 days) leaves no room to react to disruption",
                    "Qualification of a new textile supplier takes 8–10 weeks (lab + 3 line lots)",
                ],
                rootCauses=[
                    ("No second-source qualification programme existed for critical elastane yarn.", "PROCESS", "HIGH"),
                    ("Procurement policy did not mandate dual-sourcing for single-source critical inputs.", "MANAGEMENT", "HIGH"),
                    ("Supplier concentration risk was not tracked on the vendor scorecard.", "MEASUREMENT", "MEDIUM"),
                ],
                actions=[
                    dict(t="IMMEDIATE_CONTAINMENT", desc="Secure a 2-week elastane spot buffer via approved broker to cover the qualification window.",
                         rat="Prevents a line stop while the alternate supplier is being qualified.", owner=MEERA, role="Risk Owner",
                         due=days(d0, 10), status="COMPLETED", started=days(d0, 1), done=days(d0, 8),
                         ev="PO#NW-SPOT-4471 raised; 2.4 T elastane received and quarantined pending use.", cost=180000.0),
                    dict(t="CORRECTIVE", desc="Run lab + line trials to qualify Zheng Textiles elastane (denier, elongation, dye uptake).",
                         rat="Establishes a technically-approved second source.", owner=MEERA, role="Risk Owner",
                         due=days(d0, 30), status="IN_PROGRESS", started=days(d0, 12), done=None,
                         ev=None, cost=260000.0),
                    dict(t="CORRECTIVE", desc="Complete supplier audit and onboard Zheng Textiles in the vendor master with a quality agreement.",
                         rat="Formalises the alternate source for routine POs.", owner=RAJESH, role="Procurement Lead",
                         due=days(d0, 45), status="PROPOSED", started=None, done=None, ev=None, cost=120000.0),
                    dict(t="PREVENTIVE", desc="Add a dual-source requirement for all critical single-source inputs to the Procurement SOP.",
                         rat="Stops the concentration risk from recurring on other critical materials.", owner=PRIYA, role="Risk Champion",
                         due=days(d0, 25), status="COMPLETED", started=days(d0, 6), done=days(d0, 20),
                         ev="Procurement SOP v4.2 issued §5.3 'Dual-sourcing of critical single-source inputs'.", cost=40000.0),
                ],
                vmethod="TEST",
                vcriteria=("Zheng Textiles elastane passes 3 consecutive production lots within spec (elongation 480–560%, "
                           "no dye migration, tension within ±5%) and is approved in the vendor master."),
                vperiod=60, vdue=days(d0, 60),
                est_problem=21000000.0, est_actions=600000.0,
                cost_cats=[{"category": "Qualification & testing", "amount": 260000, "currency": "INR"},
                           {"category": "Spot buffer procurement", "amount": 180000, "currency": "INR"},
                           {"category": "Supplier audit & onboarding", "amount": 120000, "currency": "INR"},
                           {"category": "SOP / process change", "amount": 40000, "currency": "INR"}],
                contributors=[(PRIYA, "Risk Champion", "REVIEWER"), (RAJESH, "Procurement", "SUBJECT_MATTER_EXPERT"),
                              (ANAND, "CRO", "APPROVER")],
                comments=[
                    (MEERA, "NOTE", "Spot buffer received — buys us ~2 weeks. Zheng lab samples logged for trial batch A."),
                    (RAJESH, "NOTE", "Supplier audit scheduled once line-trial results are in; quality agreement drafted."),
                    (ANAND, "NOTE", "Keep this on the overdue watch — qualification must close before the buffer runs out."),
                ],
                attachments=[
                    ("EVIDENCE", "Zheng_Textiles_lab_trial_report_batchA.pdf", MEERA),
                    ("PROCEDURE", "Procurement_SOP_v4.2_dual_sourcing.pdf", PRIYA),
                ],
            )

        def spec_011(c: Capa) -> dict:
            d0 = c.detectedAt
            return dict(
                problemImpact=(
                    "Elastane cover of only 7 days against a 21-day overseas lead time means any shipment slip "
                    "translates directly into a knitting-line stoppage. A 30-day buffer removes the short-lead "
                    "exposure window."
                ),
                affectedAreas=["NW Raw Material Warehouse"],
                affectedDepartments=["Supply Chain", "Finance"],
                affectedProcesses=["Inventory planning", "S&OP"],
                rcaMethodology="5_WHY",
                rcaMethodologyRationale="Straightforward policy/parameter gap — 5-Why suffices.",
                rcaSummary=(
                    "Why did cover fall short? Safety stock for elastane was set at 7 days. Why? The policy pre-dated "
                    "the supplier's lead-time increase to 21 days. Why wasn't it updated? Reorder points are not "
                    "recomputed when supplier lead times change. Root cause: static safety-stock policy not tied to "
                    "actual supplier lead time for critical single-source inputs."
                ),
                contributingFactors=[
                    "Reorder points maintained manually, reviewed only annually",
                    "No days-of-cover KPI on the S&OP dashboard for critical inputs",
                ],
                rootCauses=[
                    ("Safety-stock policy for critical yarns set cover at 7 days — insufficient for overseas lead times.", "PROCESS", "HIGH"),
                    ("Reorder points were not recalculated after the supplier lead time rose to 21 days.", "MEASUREMENT", "MEDIUM"),
                ],
                actions=[
                    dict(t="CORRECTIVE", desc="Recalculate reorder point and safety stock to 30-day cover for all elastane SKUs.",
                         rat="Sizes the buffer to actual lead time plus review cadence.", owner=RAJESH, role="Supply Chain",
                         due=days(d0, 20), status="COMPLETED", started=days(d0, 2), done=days(d0, 16),
                         ev="Reorder parameters updated in ERP for 6 elastane SKUs; safety stock = 30-day cover.", cost=60000.0),
                    dict(t="CORRECTIVE", desc="Fund and stage the 30-day elastane buffer in the NW raw-material warehouse.",
                         rat="Physically establishes the protective stock.", owner=RAJESH, role="Supply Chain",
                         due=days(d0, 40), status="COMPLETED", started=days(d0, 10), done=days(d0, 34),
                         ev="Buffer stock built to 30-day cover; working capital ₹35 L approved by Finance.", cost=3500000.0),
                    dict(t="PREVENTIVE", desc="Add a quarterly days-of-cover review to S&OP for all single-source critical inputs.",
                         rat="Keeps buffers sized as lead times change.", owner=PRIYA, role="Risk Champion",
                         due=days(d0, 30), status="COMPLETED", started=days(d0, 8), done=days(d0, 28),
                         ev="S&OP agenda updated; first quarterly cover review completed and minuted.", cost=25000.0),
                ],
                vmethod="REVIEW_METRICS",
                vcriteria="Days-of-cover for elastane sustained at ≥30 for two consecutive months in the inventory MIS.",
                vperiod=60, vdue=days(d0, 75),
                est_problem=21000000.0, est_actions=3585000.0,
                cost_cats=[{"category": "Buffer working capital", "amount": 3500000, "currency": "INR"},
                           {"category": "ERP parameter update", "amount": 60000, "currency": "INR"},
                           {"category": "S&OP process change", "amount": 25000, "currency": "INR"}],
                contributors=[(PRIYA, "Risk Champion", "REVIEWER"), (MEERA, "Risk Owner", "SUBJECT_MATTER_EXPERT"),
                              (ANAND, "CRO", "APPROVER")],
                comments=[
                    (RAJESH, "NOTE", "Buffer fully staged; ERP now auto-flags reorder at 30-day cover."),
                    (PRIYA, "NOTE", "Quarterly cover review added to S&OP — first pass complete."),
                ],
                attachments=[
                    ("EVIDENCE", "Elastane_reorder_point_recalculation.xlsx", RAJESH),
                ],
            )

        def spec_012(c: Capa) -> dict:
            d0 = c.detectedAt
            return dict(
                problemImpact=(
                    "Supply ran on rolling purchase orders with no volume or continuity guarantee, leaving NW with "
                    "no contractual protection against allocation cuts or price shocks. A binding agreement with "
                    "penalty and priority-allocation clauses materially reduces continuity risk."
                ),
                affectedAreas=["NW Integrated Manufacturing Unit"],
                affectedDepartments=["Procurement", "Legal", "Supply Chain"],
                affectedProcesses=["Contracting", "Supplier management"],
                rcaMethodology="5_WHY",
                rcaMethodologyRationale="Governance/contract-strategy gap — 5-Why is adequate.",
                rcaSummary=(
                    "Why is supply unprotected? No binding long-term agreement exists. Why? Supply ran on rolling POs. "
                    "Why? Contract strategy never required continuity terms for critical single-source suppliers. "
                    "Root cause: absence of a continuity-and-penalty contracting standard for critical single-source inputs."
                ),
                contributingFactors=[
                    "Legal template library had no continuity/penalty clause for supply agreements",
                    "Category strategy focused on unit price, not continuity of supply",
                ],
                rootCauses=[
                    ("No binding long-term supply agreement existed; supply ran on rolling POs with no continuity guarantee.", "PROCESS", "HIGH"),
                    ("Contract strategy did not require continuity / penalty clauses for critical single-source suppliers.", "MANAGEMENT", "HIGH"),
                ],
                actions=[
                    dict(t="CORRECTIVE", desc="Negotiate a 24-month supply agreement with committed volumes and lead-time SLAs.",
                         rat="Locks in volume and service levels.", owner=MEERA, role="Risk Owner",
                         due=days(d0, 35), status="COMPLETED", started=days(d0, 5), done=days(d0, 30),
                         ev="24-month agreement negotiated; volumes and 14-day lead-time SLA agreed with supplier.", cost=150000.0),
                    dict(t="CORRECTIVE", desc="Include stock-out penalty and priority-allocation clauses in the agreement.",
                         rat="Creates contractual protection against allocation cuts.", owner=MEERA, role="Risk Owner",
                         due=days(d0, 35), status="COMPLETED", started=days(d0, 6), done=days(d0, 31),
                         ev="Clauses 7 (penalty) & 9 (priority allocation) executed; countersigned copy on file.", cost=120000.0),
                    dict(t="PREVENTIVE", desc="Adopt a continuity-clause template for all critical single-source contracts.",
                         rat="Standardises protection across the category.", owner=PRIYA, role="Risk Champion",
                         due=days(d0, 30), status="COMPLETED", started=days(d0, 7), done=days(d0, 27),
                         ev="Legal template library updated with continuity + penalty clause set (LEG-TMP-118).", cost=45000.0),
                ],
                vmethod="AUDIT_CHECK",
                vcriteria="Signed agreement on file carrying penalty + priority-allocation clauses; legal review closed with no open points.",
                vperiod=90, vdue=days(d0, 45),
                est_problem=21000000.0, est_actions=315000.0,
                cost_cats=[{"category": "Contract negotiation", "amount": 150000, "currency": "INR"},
                           {"category": "Legal review & clauses", "amount": 120000, "currency": "INR"},
                           {"category": "Template standardisation", "amount": 45000, "currency": "INR"}],
                contributors=[(PRIYA, "Risk Champion", "REVIEWER"), (RAJESH, "Procurement", "SUBJECT_MATTER_EXPERT"),
                              (ANAND, "CRO", "APPROVER")],
                comments=[
                    (MEERA, "NOTE", "Agreement executed with penalty + priority-allocation clauses — continuity risk materially reduced."),
                    (ANAND, "NOTE", "Good template outcome — continuity clause now standard for all critical single-source contracts."),
                ],
                attachments=[
                    ("EVIDENCE", "Elastane_supply_agreement_24mo_signed.pdf", MEERA),
                    ("PROCEDURE", "Legal_template_LEG-TMP-118_continuity_clause.pdf", PRIYA),
                ],
            )

        SPECS = {"CAPA-RTM-2026-NW-010": spec_010, "CAPA-RTM-2026-NW-011": spec_011, "CAPA-RTM-2026-NW-012": spec_012}

        for num in NUMBERS:
            capa = capas.get(num)
            if capa is None:
                print(f"[SKIP] {num} not found")
                continue
            s = SPECS[num](capa)
            d0 = capa.detectedAt
            now = datetime.now(timezone.utc)

            # idempotent: clear the children this seed manages (scoped to this capa)
            for Model in MANAGED_CHILDREN:
                await db.execute(delete(Model).where(Model.capaId == capa.id))

            # ── main-record fields (never touch state/owner/severity/dates that ERM keys on) ──
            capa.problemImpact = s["problemImpact"]
            capa.affectedAreas = s["affectedAreas"]
            capa.affectedDepartments = s["affectedDepartments"]
            capa.affectedProcesses = s["affectedProcesses"]
            capa.relatedCapaIds = others(num)

            # Fix the source-record deep link: the ERM treatment spawn wrote
            # `/erm/risks/{id}` which is a 404 (no such route). The real risk
            # detail route is `/erm/register/{id}` — so "Open source record →"
            # on the CAPA Source tab actually resolves.
            if capa.sourceReferenceId:
                capa.sourceReferenceUrl = f"/erm/register/{capa.sourceReferenceId}"

            # RCA
            capa.rcaMethodology = s["rcaMethodology"]
            capa.rcaMethodologyRationale = s["rcaMethodologyRationale"]
            capa.rcaSummary = s["rcaSummary"]
            capa.contributingFactors = s["contributingFactors"]
            capa.rcaCompleted = True
            capa.rcaCompletedAt = days(d0, 5)
            capa.rcaCompletedByUserId = MEERA
            capa.rcaDueDate = days(d0, 7)
            capa.correctiveActionDueDate = days(d0, 35)
            capa.preventiveActionDueDate = days(d0, 30)

            # Verification plan (result left open — these are still in progress)
            capa.verificationMethodId = vm.get(s["vmethod"])
            capa.verificationSuccessCriteria = s["vcriteria"]
            capa.measurementPeriodDays = s["vperiod"]
            capa.verificationDueDate = s["vdue"]

            # Cost
            capa.estimatedProblemCost = s["est_problem"]
            capa.estimatedProblemCurrency = "INR"
            capa.estimatedActionsCost = s["est_actions"]
            capa.estimatedActionsCurrency = "INR"
            capa.costCategories = s["cost_cats"]

            capa.updatedByUserId = ANAND

            # ── children ──
            for i, rc in enumerate(s["rootCauses"]):
                db.add(CapaRootCause(capaId=capa.id, description=rc[0], category=rc[1], confidence=rc[2], sortOrder=i))

            for i, a in enumerate(s["actions"]):
                db.add(CapaAction(
                    capaId=capa.id, actionType=a["t"], description=a["desc"], rationale=a["rat"],
                    ownerUserId=a["owner"], ownerRole=a["role"], dueDate=a["due"], status=a["status"],
                    startedAt=a["started"], completedAt=a["done"], evidenceOfCompletion=a["ev"],
                    costEstimate=a["cost"], costEstimateCurrency="INR", sortOrder=i,
                ))

            for u, role, ctype in s["contributors"]:
                if u:
                    db.add(CapaContributor(capaId=capa.id, userId=u, role=role, contributionType=ctype))

            for j, (author, ctype, body) in enumerate(s["comments"]):
                if author:
                    db.add(CapaComment(capaId=capa.id, authorUserId=author, commentType=ctype, body=body,
                                       isInternal=True, createdAt=days(d0, 4 + j)))

            # NOTE: no attachments are seeded. There is no CAPA file-upload flow
            # / real object storage for these, so any seeded fileUrl would 404
            # when clicked on the Linkages tab. The delete above (CapaAttachment is
            # in MANAGED_CHILDREN) also removes previously-seeded placeholder rows.

            print(f"[OK]   {num}: RCA + {len(s['actions'])} actions + {len(s['rootCauses'])} root-causes "
                  f"+ {len(s['contributors'])} contributors + {len(s['comments'])} comments; "
                  f"sourceUrl -> {capa.sourceReferenceUrl}")

        await db.commit()
        print("Committed. Refresh the CAPA pages to see full data.")


if __name__ == "__main__":
    asyncio.run(main())
