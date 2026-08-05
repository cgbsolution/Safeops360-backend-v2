"""Daily Alert Brief — deterministic demo state (build spec 2.4).

Resets to the golden-thread demo pre-state so the three live actions light up
the /dashboard/daily feed in real time. Meridian North Works (NW) stands in for
"Factory 7" (DECISIONS.md D18); apparel areas were seeded by seed-capture.ts.

Pre-state this script establishes:
  • RCA-2026-0104 — boiler steam leak, NW Boiler House, status PEER_REVIEW
    (ready to APPROVE = action A), with 3 linked CAPAs (ENTERPRISE_RCA source,
    open, staggered due dates).
  • PTW-NW-2026-2231 — HOT_WORK, Cutting Hall, ACTIVE (ready to SUSPEND =
    action B), plus PTW-NW-2026-2232 (overlapping ACTIVE permit in Cutting Hall
    so the suspension flags an overlap).
  • Two machine-guarding / Sewing Line 2 field reports already triaged HIGH, so
    submitting + triaging a 3rd (action C) crosses the cluster threshold (>=3).

The live demo actions (performed through the real UI):
  A  Approve RCA-2026-0104  → ATTENTION "RCA-2026-0104 closed → 3 corrective actions now active"
  B  Suspend PTW-NW-2026-2231 → CRITICAL "…suspended → 1 overlapping permit flagged" (Cutting Hall)
  C  Submit + triage-HIGH a 3rd machine-guarding report in Sewing Line 2 → cluster alert

Idempotent: matches by stable business numbers and RESETS in place (governed
entities can't be hard-deleted). Clears prior demo alerts + unprocessed events
so the feed starts clean.

    python seed_demo_brief.py     (⚠ backend .env DATABASE_URL is the live DB)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.db import AsyncSessionLocal
from app.models.capa import Capa, CapaSourceCategory, CapaSourceType
from app.models.capture import CaptureSubmission
from app.models.permit import Permit, PermitStatus, PermitType
from app.models.plant import Area, Plant
from app.models.rca import RootCauseAnalysis
from app.models.user import User
from app.services import capture as cap

RCA_CODE = "RCA-2026-0104"
PTW_MAIN = "PTW-NW-2026-2231"
PTW_OVERLAP = "PTW-NW-2026-2232"
CLUSTER_PREFIX = "demo-brief-cluster-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _area(db, plant_id: str, name: str) -> Area | None:
    return (
        await db.execute(select(Area).where(Area.plantId == plant_id).where(Area.name == name))
    ).scalar_one_or_none()


async def main() -> None:
    async with AsyncSessionLocal() as db:
        now = _now()
        nw = (await db.execute(select(Plant).where(Plant.code == "NW"))).scalar_one_or_none()
        if nw is None:
            raise SystemExit("Plant NW not found — run the base seeds first.")

        boiler = await _area(db, nw.id, "Boiler House")
        cutting = await _area(db, nw.id, "Cutting Hall")
        sewing2 = await _area(db, nw.id, "Sewing Line 2")
        if not (boiler and cutting and sewing2):
            raise SystemExit("Apparel areas missing — run `npm run db:seed-capture` first.")

        officer = (
            await db.execute(select(User).where(User.email == "priya.nair@safeops360.in"))
        ).scalar_one_or_none() or (
            await db.execute(select(User).where(User.plantId == nw.id).limit(1))
        ).scalar_one_or_none()
        if officer is None:
            raise SystemExit("No demo user at NW.")

        # ── clean prior demo alerts + unprocessed events (feed starts clean) ──
        await db.execute(
            text(
                'UPDATE "Alert" SET "isDeleted"=true, "deletedAt"=now() '
                "WHERE \"dedupeKey\" LIKE :a OR \"dedupeKey\" LIKE :b OR \"dedupeKey\" LIKE :c"
            ),
            {"a": f"%{RCA_CODE}%", "b": "%:cutting%", "c": "cluster:%"},
        )
        # events reference entity ids; clear by the demo RCA/PTW once we know them below

        # ── RCA-2026-0104 (PEER_REVIEW, ready to approve) ──
        rca = (
            await db.execute(select(RootCauseAnalysis).where(RootCauseAnalysis.rcaCode == RCA_CODE))
        ).scalar_one_or_none()
        payload = {
            "method": "FIVE_WHY",
            "whys": [
                {"q": "Why did the boiler steam line leak?", "a": "Gasket on the E-104 flange failed."},
                {"q": "Why did the gasket fail?", "a": "It was past its rated service life."},
                {"q": "Why was it past life?", "a": "PM replacement interval was skipped last cycle."},
                {"q": "Why skipped?", "a": "No mechanic was available on the shift."},
                {"q": "Why unavailable?", "a": "Maintenance staffing plan had no boiler-house cover."},
            ],
        }
        if rca is None:
            rca = RootCauseAnalysis(
                rcaCode=RCA_CODE,
                title="Boiler House steam leak — flange gasket failure (E-104)",
                originType="EVENT",
                primaryDomain="OPERATIONAL",
                methodology="FIVE_WHY",
                status="PEER_REVIEW",
                analysisPayload=payload,
                narrative="Steam release at the E-104 heat-exchanger flange during a hot-work window.",
                analystId=officer.id,
                plantId=nw.id,
                createdBy=officer.id,
            )
            db.add(rca)
            await db.flush()
        else:
            rca.status = "PEER_REVIEW"
            rca.approverId = None
            rca.approvedAt = None
            rca.analysisPayload = payload
            rca.plantId = nw.id
            rca.isDeleted = False
        await db.flush()

        # ── 3 linked CAPAs (ENTERPRISE_RCA), open, staggered due dates ──
        # Fixed numbers (spec's CAPA-88/89/90 → platform format, DECISIONS.md D18)
        # built directly so re-runs are collision-free (spawn_capa's count-based
        # numbering can clash with pre-existing rows).
        st = (
            await db.execute(select(CapaSourceType).where(CapaSourceType.code == "ENTERPRISE_RCA"))
        ).scalar_one_or_none()
        if st is None:
            raise SystemExit("ENTERPRISE_RCA CAPA source type not seeded — run seed_rca.py first.")
        scat = await db.get(CapaSourceCategory, st.categoryId)
        plans = [
            ("088", "Replace E-104 flange gasket set + torque to spec", 7),
            ("089", "Reinstate boiler-house PM schedule with shift cover", 14),
            ("090", "Add boiler-house mechanic to the shift staffing plan", 21),
        ]
        for suffix, title, days in plans:
            number = f"CAPA-{scat.prefix if scat else 'S'}-2026-NW-{suffix}"
            existing = (
                await db.execute(select(Capa).where(Capa.capaNumber == number))
            ).scalar_one_or_none()
            if existing is not None:
                existing.state = "ACTIONS_PLANNED"
                existing.closureTargetDate = now + timedelta(days=days)
                existing.sourceReferenceId = rca.id
                existing.isDeleted = False
                continue
            db.add(
                Capa(
                    capaNumber=number,
                    title=title,
                    plantId=nw.id,
                    sourceCategoryId=st.categoryId,
                    sourceTypeId=st.id,
                    sourceTypeCode="ENTERPRISE_RCA",
                    sourceReferenceId=rca.id,
                    sourceReferenceUrl=f"/erm/rca/{rca.id}",
                    sourceReferenceSummary=rca.title,
                    problemDescription=f"Corrective action from {RCA_CODE} (boiler steam leak).",
                    detectionMethod="RCA",
                    detectedAt=now,
                    detectedByUserId=officer.id,
                    primaryCategory=scat.name if scat else "SAFETY",
                    severity="CRITICAL",
                    priority="HIGH",
                    state="ACTIONS_PLANNED",
                    stateChangedAt=now,
                    closureTargetDate=now + timedelta(days=days),
                    raisedByUserId=officer.id,
                    primaryOwnerUserId=officer.id,
                    createdByUserId=officer.id,
                )
            )
        capa_count = 3
        await db.flush()

        # ── PTW-NW-2026-2231 ACTIVE in Cutting Hall + one overlap ──
        async def _upsert_permit(number: str, desc_loc: str) -> Permit:
            p = (await db.execute(select(Permit).where(Permit.number == number))).scalar_one_or_none()
            if p is None:
                p = Permit(
                    number=number,
                    type=PermitType.HOT_WORK,
                    plantId=nw.id,
                    areaId=cutting.id,
                    location=desc_loc,
                    scopeOfWork=desc_loc,
                    validFrom=now - timedelta(hours=2),
                    validTo=now + timedelta(hours=8),
                    originatorId=officer.id,
                    issuerId=officer.id,
                    receiverId=officer.id,
                    status=PermitStatus.ACTIVE,
                )
                db.add(p)
            else:
                p.status = PermitStatus.ACTIVE
                p.areaId = cutting.id
                p.suspendedAt = None
                p.suspendedReason = None
                p.isCurrentlySuspended = False
                p.validFrom = now - timedelta(hours=2)
                p.validTo = now + timedelta(hours=8)
            await db.flush()
            return p

        await _upsert_permit(PTW_MAIN, "Cutting Hall — Heat Exchanger E-104 hot work")
        await _upsert_permit(PTW_OVERLAP, "Cutting Hall — adjacent duct welding")

        # ── 2 prior HIGH machine-guarding reports in Sewing Line 2 (cluster seed) ──
        mg = await cap.resolve_code(db, "HAZARD", "machine_guarding")
        snap = (
            {"l1": {"code": mg.code, "labels": mg.labels, "iconKey": mg.iconKey}, "l2": None}
            if mg
            else None
        )
        # gapless numbering off the current max suffix (count-based numbering
        # collides against gaps left by prior test submissions)
        max_suffix = (
            await db.execute(
                text(
                    "SELECT COALESCE(MAX(CAST(SPLIT_PART(number,'-',4) AS INT)),0) "
                    "FROM \"CaptureSubmission\" WHERE number LIKE 'FLD-2026-NW-%'"
                )
            )
        ).scalar() or 0
        for i in range(2):
            cid = f"{CLUSTER_PREFIX}{i}"
            existing = await cap.find_existing(db, cid)
            if existing is not None:
                existing.status = "triaged"
                existing.riskLevel = "HIGH"
                existing.hiraLikelihood = 4
                existing.hiraSeverity = 4
                existing.riskScore = 16
                existing.areaId = sewing2.id
                existing.categoryL1Id = mg.id if mg else None
                existing.categorySnapshot = snap
                existing.isDeleted = False
                continue
            num = f"FLD-2026-NW-{max_suffix + 1 + i:04d}"
            db.add(
                CaptureSubmission(
                    number=num,
                    clientSubmissionId=cid,
                    type="unsafe_condition",
                    reporterId=officer.id,
                    plantId=nw.id,
                    areaId=sewing2.id,
                    categoryL1Id=mg.id if mg else None,
                    categorySnapshot=snap,
                    severitySelfReported="high",
                    status="triaged",
                    triagedById=officer.id,
                    triagedAt=now,
                    hiraLikelihood=4,
                    hiraSeverity=4,
                    riskScore=16,
                    riskLevel="HIGH",
                    createdAtClient=now,
                )
            )
        await db.flush()

        # clear any unprocessed events for the demo entities so nothing pre-fires
        await db.execute(
            text('DELETE FROM "DomainEvent" WHERE "entityId" = :rid OR "entityRef" IN (:p1, :p2)'),
            {"rid": rca.id, "p1": PTW_MAIN, "p2": PTW_OVERLAP},
        )

        await db.commit()

        print("✅ Daily Brief demo state ready (Meridian North Works):")
        print(f"   RCA  {RCA_CODE}  status=PEER_REVIEW  ({capa_count} linked CAPAs)  → approve = action A")
        print(f"   PTW  {PTW_MAIN}  ACTIVE (Cutting Hall) + {PTW_OVERLAP} overlap  → suspend = action B")
        print("   2× HIGH machine-guarding reports in Sewing Line 2  → triage a 3rd = action C (cluster)")
        print()
        print("   Demo flow: open /dashboard/daily, then perform A/B/C. With SCHEDULER_ENABLED=true")
        print("   the alerts_impact_resolver (60s) materialises the cards; or POST /api/jobs/alerts_impact_resolver/run.")


if __name__ == "__main__":
    asyncio.run(main())
