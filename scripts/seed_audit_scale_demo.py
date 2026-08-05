"""Seed: a ~1500-checkpoint audit for the scale + multi-auditor story.

Imports a synthetic 15-discipline × 100-checkpoint library, schedules an audit
scoped to it with 3 co-auditors (by discipline) + 3 auditees (by discipline),
bulk-passes most disciplines, records a handful of evidenced fails, and submits
— so /cams/audits shows a real ≈1500-checkpoint audit running end-to-end on the
paginated conduct + report machinery.

Idempotent on the library (upsert); the audit is created once (guarded by title).

    .venv/Scripts/python.exe scripts/seed_audit_scale_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.plant import Plant
from app.models.user import User
from app.services import audit_compliance as svc

INDUSTRY = "SCALE_DEMO_1500"
TITLE = "Scale Demo — 1500 Checkpoints"
N_DISC = 15
N_PER = 100
_COLORS = ["#ef4444", "#f59e0b", "#10b981", "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
           "#6366f1", "#84cc16", "#06b6d4", "#a855f7", "#eab308", "#22c55e", "#0ea5e9"]


def _build_library() -> dict:
    cats = []
    for di in range(N_DISC):
        code = f"DISC-{di + 1:02d}"
        checkpoints = []
        for ci in range(N_PER):
            crit = ["critical", "major", "minor", "observation"][ci % 4]
            checkpoints.append({
                "code": f"{code}-{ci + 1:03d}",
                "question": f"[{code}] Verify control point {ci + 1} is implemented and effective.",
                "guidance": "Assess against the applicable standard and record evidence.",
                "requirement_reference": f"REQ-{di + 1}.{ci + 1}",
                "standard": ["ISO 45001", "ISO 14001", "ISO 9001"][di % 3],
                "criticality": crit,
                "response_type": "pass_partial_fail",
                "requires_photo_on_fail": crit == "critical",
            })
        cats.append({
            "category_code": code, "category_name": f"Discipline {di + 1}",
            "category_color": _COLORS[di % len(_COLORS)], "checkpoints": checkpoints,
        })
    return {"industryCode": INDUSTRY, "industryName": "Scale Demo (1500-CP)", "version": "2026.1", "categories": cats}


async def main() -> None:
    async with AsyncSessionLocal() as db:
        plant = (await db.execute(select(Plant).where(Plant.code == "NW"))).scalars().first()
        if plant is None:
            print("No NW plant — aborting.")
            return
        users = (await db.execute(select(User).where(User.plantId == plant.id).limit(7))).scalars().all()
        if len(users) < 7:
            users = (await db.execute(select(User).limit(7))).scalars().all()
        lead = users[0]
        co = users[1:4]      # 3 co-auditors
        auditees = users[4:7]  # 3 auditees

        # 1) Import the 1500-cp library (upsert).
        lib = _build_library()
        res = await svc.import_library(db, user=lead, payload=lib)
        print(f"Library: {res['industryCode']} — {res['checkpointCount']} checkpoints / {res['disciplines']} disciplines")

        # Guard: only create the audit once.
        existing = (await db.execute(select(ComplianceAudit).where(ComplianceAudit.title == TITLE))).scalars().first()
        if existing is not None:
            print(f"Audit already exists: {existing.auditNumber} ({existing.totalCheckpoints} cp) — committing library only.")
            await db.commit()
            return

        disc_codes = [c["category_code"] for c in lib["categories"]]
        # Distribute disciplines: 5 to each co-auditor (rest → lead), 5 to each auditee.
        co_assign = [{"userId": co[i].id, "disciplineIds": disc_codes[i * 5:(i + 1) * 5]} for i in range(3)]
        auditee_assign = [{"userId": auditees[i].id, "responsibleCategories": disc_codes[i * 5:(i + 1) * 5]} for i in range(3)]

        audit = await svc.create_audit(db, user=lead, data={
            "title": TITLE, "plantId": plant.id, "industryCode": INDUSTRY,
            "selectedDisciplineIds": disc_codes, "scheduledDate": datetime.now(timezone.utc),
            "leadAuditorUserId": lead.id, "plantManagerUserId": users[0].id,
            "coAuditors": co_assign, "auditees": auditee_assign,
        })
        print(f"Audit: {audit.auditNumber} — {audit.totalCheckpoints} checkpoints materialized")

        # 2) Bulk-pass the first 13 disciplines (the fast path), evidence a few fails.
        for code in disc_codes[:13]:
            await svc.bulk_save_response(db, user=lead, audit_id=audit.id, value="pass", discipline_id=code)
        # A handful of evidenced fails in discipline 14 (major) + 15 (mixed).
        ev = [{"storagePath": "audit-compliance/seed/x.jpg", "url": "x"}]
        targets = (await db.execute(
            select(AuditCheckpointResponse).where(
                AuditCheckpointResponse.auditId == audit.id,
                AuditCheckpointResponse.categoryId.in_([disc_codes[13], disc_codes[14]]),
            ).order_by(AuditCheckpointResponse.sequence).limit(220)
        )).scalars().all()
        fails = 0
        for i, r in enumerate(targets):
            if i % 20 == 0 and fails < 8:  # ~8 deliberate fails, evidenced
                await svc.save_response(db, user=lead, audit_id=audit.id, payload={
                    "checkpointCode": r.checkpointCode, "value": "fail",
                    "textObservation": "Control not effective — finding raised.", "photos": ev})
                fails += 1
            else:
                await svc.save_response(db, user=lead, audit_id=audit.id, payload={"checkpointCode": r.checkpointCode, "value": "pass"})
        print(f"Conducted: 13 disciplines bulk-passed + discipline 14/15 detailed ({fails} fails)")

        # 3) Submit — routes findings to the discipline auditees, auto-CAPA on criticals.
        sub = await svc.submit_audit(db, user=lead, audit_id=audit.id)
        print(f"Submitted: compliance {sub.get('score', {}).get('overall_score_pct')}% · CAPAs {sub.get('capasSpawned', 0)}")

        await db.commit()
        print("Committed. View at /cams/audits → open", audit.auditNumber)


if __name__ == "__main__":
    asyncio.run(main())
