"""P3-3 — Expired PPE on active permits.

The PPE gate already blocks at activation; this catches items whose service life,
inspection, or fit-test validity LAPSED *after* activation while the permit is
still live. Joins active permits → crew + permit-linked PPE issuances → the PPE
item's expiry signals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import Permit, PermitCrewMember
from app.models.ppe import PpeItem, PpeIssuance


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _expiry_reasons(item: PpeItem, now: datetime) -> list[str]:
    reasons = []
    if _aware(item.serviceLifeEndDate) and _aware(item.serviceLifeEndDate) < now:
        reasons.append(f"service life ended {item.serviceLifeEndDate.date()}")
    if _aware(item.nextInspectionDueDate) and _aware(item.nextInspectionDueDate) < now:
        reasons.append(f"inspection overdue (due {item.nextInspectionDueDate.date()})")
    if _aware(item.fitTestValidUntil) and _aware(item.fitTestValidUntil) < now:
        reasons.append(f"fit-test expired {item.fitTestValidUntil.date()}")
    if item.batchUnderRecall:
        reasons.append("batch under recall")
    return reasons


async def expired_ppe_on_active_permits(db: AsyncSession, plant_ids: list[str] | None) -> dict[str, Any]:
    now = _now()
    pq = select(Permit).where(Permit.isDeleted.is_(False)).where(Permit.status.in_(("ACTIVE", "SUSPENDED")))
    if plant_ids is not None:
        pq = pq.where(Permit.plantId.in_(plant_ids or ["__none__"]))
    permits = (await db.execute(pq)).scalars().all()
    if not permits:
        return {"alerts": [], "permitsAffected": 0, "totalExpiredItems": 0}
    permit_ids = [p.id for p in permits]

    # crew per permit
    crew = (await db.execute(select(PermitCrewMember).where(PermitCrewMember.permitId.in_(permit_ids)))).scalars().all()
    crew_by_permit: dict[str, list[str]] = {}
    user_to_permits: dict[str, list[str]] = {}
    for c in crew:
        crew_by_permit.setdefault(c.permitId, []).append(c.userId)
        user_to_permits.setdefault(c.userId, []).append(c.permitId)

    # active issuances either linked to the permit OR held by its crew
    crew_users = list(user_to_permits.keys())
    issuances = (
        await db.execute(
            select(PpeIssuance).where(PpeIssuance.status == "active")
            .where((PpeIssuance.linkedPermitId.in_(permit_ids)) | (PpeIssuance.issuedToUserId.in_(crew_users or ["__none__"])))
        )
    ).scalars().all()
    item_ids = list({i.ppeItemId for i in issuances})
    items = {it.id: it for it in (await db.execute(select(PpeItem).where(PpeItem.id.in_(item_ids or ["__none__"])))).scalars().all()}
    permit_by_id = {p.id: p for p in permits}

    alerts: list[dict[str, Any]] = []
    affected_permits = set()
    for iss in issuances:
        item = items.get(iss.ppeItemId)
        if not item:
            continue
        reasons = _expiry_reasons(item, now)
        if not reasons:
            continue
        # which active permit(s) this issuance touches
        target_permits = [iss.linkedPermitId] if iss.linkedPermitId in permit_by_id else user_to_permits.get(iss.issuedToUserId, [])
        for pid in target_permits:
            p = permit_by_id.get(pid)
            if not p:
                continue
            affected_permits.add(pid)
            alerts.append({
                "permitId": pid, "permitNumber": getattr(p, "permitNumber", None) or pid[:8], "plantId": p.plantId,
                "workerUserId": iss.issuedToUserId, "workerName": iss.issuedToName,
                "ppeType": iss.ppeTypeName, "serialNumber": iss.serialNumber,
                "reasons": reasons,
            })
    return {"alerts": alerts, "permitsAffected": len(affected_permits), "totalExpiredItems": len(alerts)}
