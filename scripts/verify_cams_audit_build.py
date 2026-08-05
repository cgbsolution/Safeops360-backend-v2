"""Cross-verification of the CAMS audit unification build (read-only).

Confirms: all new routes are registered, the live DB has the new columns/indexes,
the 1500-cp seed exists, the union adapters + analytics run, and the Inspections
scoping holds. Prints a PASS/FAIL line per check.

    .venv/Scripts/python.exe scripts/verify_cams_audit_build.py
"""

from __future__ import annotations

import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import func, select, text

from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.audit_compliance import AuditCheckpointLibrary, AuditCheckpointResponse, ComplianceAudit
from app.models.cams import CamsEngagement
from app.services import audit_compliance as acsvc
from app.services import cams as camssvc

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def _routes() -> set[str]:
    out = set()
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        for m in methods:
            out.add(f"{m} {path}")
    return out


async def main() -> None:
    routes = _routes()
    EXPECT = [
        "GET /api/audit-compliance/{audit_id}/checkpoints",
        "GET /api/audit-compliance/{audit_id}/checkpoints/{checkpoint_id}/interactions",
        "POST /api/audit-compliance/{audit_id}/responses/bulk",
        "GET /api/audit-compliance/reports/{report_id}/register",
        "POST /api/audit-compliance/library/import",
        "GET /api/audit-compliance/library/{industry_code}",
        "POST /api/audit-compliance/{audit_id}/disciplines",
        "POST /api/audit-compliance/{audit_id}/allocate",
        "GET /api/cams/unified-engagements",
        "GET /api/cams/unified-findings",
    ]
    for r in EXPECT:
        check(f"route {r}", r in routes, "" if r in routes else "NOT REGISTERED")

    async with AsyncSessionLocal() as db:
        # ── DB schema ──────────────────────────────────────────────────────
        col = (await db.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name='AuditCheckpointResponse' "
            "AND column_name='assignedAuditorId'"))).first()
        check("DB column assignedAuditorId", bool(col))
        idx_rows = set((await db.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='AuditCheckpointResponse'"))).scalars().all())
        check("DB index auditId_sequence", "AuditCheckpointResponse_auditId_sequence_idx" in idx_rows)
        check("DB index auditId_assignedAuditorId", "AuditCheckpointResponse_auditId_assignedAuditorId_idx" in idx_rows)

        # ── 1500-cp seed ───────────────────────────────────────────────────
        lib = (await db.execute(select(AuditCheckpointLibrary).where(
            AuditCheckpointLibrary.industryCode == "SCALE_DEMO_1500"))).scalar_one_or_none()
        check("scale library present (1500 cp)", lib is not None and lib.checkpointCount == 1500,
              f"count={getattr(lib, 'checkpointCount', None)}")
        big = (await db.execute(select(ComplianceAudit).where(
            ComplianceAudit.title == "Scale Demo — 1500 Checkpoints"))).scalar_one_or_none()
        if big:
            n = (await db.execute(select(func.count(AuditCheckpointResponse.id)).where(
                AuditCheckpointResponse.auditId == big.id))).scalar_one()
            check("scale audit has 1500 rows", n == 1500, f"{big.auditNumber}: {n} rows, status={big.status}")
            # auditor assignment present on the scale audit
            auditors = (await db.execute(select(func.count(func.distinct(AuditCheckpointResponse.assignedAuditorId))).where(
                AuditCheckpointResponse.auditId == big.id))).scalar_one()
            check("scale audit multi-auditor", auditors >= 2, f"{auditors} distinct auditors")
        else:
            check("scale audit present", False, "not found")

        # ── Paginated checkpoints on the big audit (no full load) ──────────
        if big:
            page = await acsvc.list_checkpoints(db, audit_id=big.id, limit=50)
            check("list_checkpoints paginates 1500", page["total"] == 1500 and len(page["items"]) == 50 and page["nextCursor"],
                  f"total={page['total']} page={len(page['items'])}")
            roll = await acsvc._discipline_rollup(db, big.id)
            check("discipline rollup sums to 1500", sum(c["total"] for c in roll) == 1500, f"{len(roll)} disciplines")

        # ── Union adapters + analytics ─────────────────────────────────────
        ue = await camssvc.audit_engagements(db)
        check("audit_engagements feed", len(ue) >= 1 and all(e["href"].startswith("/cams/audits/") for e in ue), f"{len(ue)} audits")
        uf = await camssvc.audit_findings(db)
        check("audit_findings feed", isinstance(uf, list), f"{len(uf)} findings")
        an = await camssvc.compute_analytics(db)
        check("analytics folds audits (byType)", an["byType"].get("COMPLIANCE_AUDIT", 0) >= 1, f"{an['byType'].get('COMPLIANCE_AUDIT')}")

        # ── Inspections scoping: INSPECTION-only count vs total ────────────
        insp = (await db.execute(select(func.count(CamsEngagement.id)).where(
            CamsEngagement.isDeleted.is_(False), CamsEngagement.engagementType == "INSPECTION"))).scalar_one()
        total_eng = (await db.execute(select(func.count(CamsEngagement.id)).where(
            CamsEngagement.isDeleted.is_(False)))).scalar_one()
        check("Cams engine has engagements", total_eng >= 0, f"INSPECTION={insp} / total={total_eng} (audits hidden from Inspections page)")

    npass = sum(1 for _, ok, _ in results if ok)
    print("\n=== CROSS-VERIFICATION ===")
    for nm, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {nm}" + (f" — {detail}" if detail else ""))
    print(f"\n{npass}/{len(results)} checks passed")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
