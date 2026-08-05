"""DB-backed verification of the ERM RCA module against the seeded database.

Covers the acceptance behaviours that need a real DB (the pure analytic core is in
tests/test_rca.py). Everything runs inside ONE session and ROLLS BACK at the end —
it never mutates the demo data.

  RCA-T01 risk-derived RCA (no incident)         RCA-T13 domain-filtered analytics
  RCA-T11 contributing causes for a risk         RCA-T15 cause-to-risk graph (isolation ≥3 risks)
  RCA-T18 tenant isolation                       RCA-T19 raise CAPA (ENTERPRISE_RCA back-link)
  RCA-T20 soft-delete removes from analytics

    python verify_rca.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.soft_delete import soft_delete
from app.models.erm import EnterpriseRisk
from app.models.rca import RcaRiskLink, RootCauseAnalysis, RootCauseSubCause
from app.services import rca_analytics, rca_core
from app.services.access_scope import system_scope
from app.services.capa_spawn import spawn_capa

results: list[tuple[str, bool, str]] = []


def check(tid: str, ok: bool, detail: str = "") -> None:
    results.append((tid, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tid} {detail}")


async def main() -> None:
    async with AsyncSessionLocal() as db:
        scope = system_scope([], all_plants=True, job_name="verify-rca")

        # ── RCA-T01: risk-derived RCA, no incident ──
        risk = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)).limit(1))).scalar_one()
        rca = await rca_core.create_risk_rca(db, source_risk_id=risk.id, title="verify risk RCA", actor_id="verify")
        await db.flush()
        check("RCA-T01", rca.originType == "RISK" and rca.sourceEventId is None and rca.sourceRiskId == risk.id
              and rca.id is not None, f"(domain={rca.primaryDomain})")

        # ── RCA-T19: raise CAPA from the RCA → ENTERPRISE_RCA back-link ──
        # Isolated in a savepoint so any failure can't poison the outer session.
        try:
            async with db.begin_nested():
                capa = await spawn_capa(db, source_code="ENTERPRISE_RCA", plant_id=rca.plantId,
                                        title="verify capa", problem="verify", ref_id=rca.id,
                                        ref_url=f"/erm/rca/{rca.id}", detected_method="RCA",
                                        owner_id=risk.riskOwnerId, actor_id=risk.riskOwnerId)
                await db.flush()
                check("RCA-T19", capa is not None and capa.sourceTypeCode == "ENTERPRISE_RCA"
                      and capa.sourceReferenceId == rca.id, f"({capa.capaNumber})")
        except Exception as e:  # noqa: BLE001
            check("RCA-T19", False, f"(spawn_capa raised: {e})")

        # ── RCA-T11: contributing causes for a seeded, linked risk ──
        seed_link = (await db.execute(select(RcaRiskLink).limit(1))).scalar_one_or_none()
        if seed_link is not None:
            res = await rca_analytics.compute_contributing_causes_for_risk(db, seed_link.riskId, scope)
            check("RCA-T11", len(res["causes"]) >= 1 and all(c["count"] >= 1 for c in res["causes"]),
                  f"({len(res['causes'])} contributing causes)")
        else:
            check("RCA-T11", False, "(no seeded risk links found)")

        # ── RCA-T13: domain-filtered analytics excludes operational-only causes ──
        cmp_only = await rca_analytics.compute_cause_analytics(db, scope, domain_filter="COMPLIANCE")
        codes = {c["subCauseCode"] for c in cmp_only["causes"]}
        ent = await rca_analytics.compute_cause_analytics(db, scope)  # enterprise-wide
        all_codes = {c["subCauseCode"] for c in ent["causes"]}
        check("RCA-T13", "OPS-ISO" not in codes and "OPS-ISO" in all_codes,
              f"(compliance causes={len(codes)}, enterprise causes={len(all_codes)})")

        # ── RCA-T15: cause-to-risk graph for isolation renders ≥3 risk nodes ──
        iso = (await db.execute(select(RootCauseSubCause).where(RootCauseSubCause.code == "OPS-ISO"))).scalar_one_or_none()
        if iso is not None:
            graph = await rca_analytics.build_cause_to_risk_graph(db, scope, sub_cause_id=iso.id)
            risk_nodes = [n for n in graph["nodes"] if n["type"] == "risk"]
            check("RCA-T15", len(risk_nodes) >= 3, f"({len(risk_nodes)} risk nodes)")
        else:
            check("RCA-T15", False, "(OPS-ISO sub-cause missing)")

        # ── RCA-T18: tenant isolation — a tenant-B RCA is invisible to the default scope ──
        rca_b = RootCauseAnalysis(
            rcaCode="RCA-VERIFY-TENANTB", title="tenant B", originType="RISK", sourceRiskId=risk.id,
            primaryDomain="OPERATIONAL", methodology="FIVE_WHY", status="APPROVED", analysisPayload={},
            analystId="verify", tenantId="tenant-B",
        )
        db.add(rca_b)
        await db.flush()
        default_ids = {r.id for r in await rca_analytics._load_approved_rcas(db, scope)}
        b_scope = system_scope([], all_plants=True, job_name="verify-rca")
        b_scope.tenant_id = "tenant-B"
        b_ids = {r.id for r in await rca_analytics._load_approved_rcas(db, b_scope)}
        check("RCA-T18", rca_b.id not in default_ids and rca_b.id in b_ids, "(tenant-B hidden from default scope)")

        # ── RCA-T20: soft-delete removes an RCA from analytics ──
        seed_rca = (await db.execute(
            select(RootCauseAnalysis).where(RootCauseAnalysis.rcaCode == "RCA-2026-9001")
        )).scalar_one()
        before = await rca_analytics.compute_cause_analytics(db, scope)
        iso_before = next((c for c in before["causes"] if c["subCauseCode"] == "OPS-ISO"), None)
        soft_delete(seed_rca, "verify", "verification soft-delete test")
        await db.flush()
        after = await rca_analytics.compute_cause_analytics(db, scope)
        iso_after = next((c for c in after["causes"] if c["subCauseCode"] == "OPS-ISO"), None)
        ok20 = iso_before is not None and (iso_after is None or iso_after["occurrences"] < iso_before["occurrences"])
        check("RCA-T20", ok20, f"(OPS-ISO occ {iso_before['occurrences'] if iso_before else '?'} -> "
                               f"{iso_after['occurrences'] if iso_after else 0})")

        # never persist verification mutations
        await db.rollback()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} DB checks passed.")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
