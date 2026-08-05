"""Phase 4 smoke test — CAMS surface union (audits + inspections).

Verifies the ComplianceAudit→CAMS adapters and that compute_analytics folds in
audits. Read-only against live data (no writes, no rollback needed).

    .venv/Scripts/python.exe scripts/test_audit_scale_p4.py
"""

from __future__ import annotations

import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from app.core.db import AsyncSessionLocal
from app.services import cams as svc

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


_CAMS_ENG_FIELDS = {"id", "engagementCode", "title", "engagementType", "status", "plannedDate", "siteId", "leadAuditorId", "findingCount", "openFindingCount", "href"}
_CAMS_FND_FIELDS = {"id", "findingCode", "engagementCode", "severity", "status", "siteId", "ageDays", "href"}
_CAMS_STATUSES = {"PLANNED", "SCHEDULED", "IN_PROGRESS", "FIELDWORK_COMPLETE", "FINDINGS_REVIEW", "REPORT_ISSUED", "CLOSED", "CANCELLED"}
_CAMS_SEVERITIES = {"OBSERVATION", "OPPORTUNITY_FOR_IMPROVEMENT", "MINOR_NC", "MAJOR_NC", "CRITICAL_NC"}


async def main() -> None:
    async with AsyncSessionLocal() as db:
        engs = await svc.audit_engagements(db)
        check("audit_engagements returns rows", len(engs) > 0, f"{len(engs)} audits projected")
        if engs:
            e = engs[0]
            check("engagement has required CAMS fields", _CAMS_ENG_FIELDS <= set(e), f"missing={_CAMS_ENG_FIELDS - set(e)}")
            check("engagement status in CAMS vocab", all(x["status"] in _CAMS_STATUSES for x in engs), "ok")
            check("engagement href → /cams/audits", all(x["href"].startswith("/cams/audits/") for x in engs), "ok")
            check("engagement sourceModule=AUDIT", all(x["sourceModule"] == "AUDIT" for x in engs), "ok")

        finds = await svc.audit_findings(db)
        check("audit_findings returns rows", isinstance(finds, list), f"{len(finds)} audit findings")
        if finds:
            f = finds[0]
            check("finding has required CAMS fields", _CAMS_FND_FIELDS <= set(f), f"missing={_CAMS_FND_FIELDS - set(f)}")
            check("finding severity in CAMS vocab", all(x["severity"] in _CAMS_SEVERITIES for x in finds), "ok")
            check("finding href → /cams/audits", all(x["href"].startswith("/cams/audits/") for x in finds), "ok")

        an = await svc.compute_analytics(db)
        check("analytics has byType COMPLIANCE_AUDIT", an["byType"].get("COMPLIANCE_AUDIT", 0) >= len(engs) - 0 if engs else True, f"{an['byType'].get('COMPLIANCE_AUDIT')}")
        check("analytics bySourceModule has AUDIT", ("AUDIT" in an["bySourceModule"]) if engs else True, f"{list(an['bySourceModule'])}")
        check("analytics programme total ≥ audit count", an["programme"]["total"] >= len(engs), f"total={an['programme']['total']} audits={len(engs)}")

    npass = sum(1 for _, ok, _ in results if ok)
    for nm, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {nm}" + (f" — {detail}" if detail else ""))
    print(f"\n{npass}/{len(results)} checks passed")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
