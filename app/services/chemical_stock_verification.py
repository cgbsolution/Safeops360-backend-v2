"""Periodic chemical stock verification (spec §4.6).

Deliberately thin. The whole design intent of this file is what it does NOT
contain: there is no inspection engine here, no checklist model, no findings
table, no scheduling loop. Stock verification is a `CamsEngagement` of type
`CHEMICAL_STOCK_VERIFICATION`, run through the existing CAMS audit engine — the
same reuse principle Fire & Life Safety follows, and the thing acceptance
criterion #5 checks for ("verify no parallel audit engine was created").

Everything the engine already provides comes for free and would have to be
rebuilt otherwise: auditor independence (an auditor cannot approve their own
findings, which is precisely the control a stock count needs), competence
checks, the offline `/capture` PWA for counting in a store with no signal,
findings → CAPA, evidence attachment, and the annual programme.

What this module adds is the chemical-specific part: turning a site's live
inventory into the count sheet, and reconciling a submitted count against the
ledger — producing an ADJUSTMENT transaction rather than an edit, so the
discrepancy stays visible (business rule §5).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cams import CamsAuditType, CamsEngagement
from app.models.chemical import ChemicalInventoryItem, ChemicalMaster, ChemicalStorageLocation

logger = logging.getLogger(__name__)

AUDIT_TYPE_CODE = "CHEMICAL_STOCK_VERIFICATION"
SOURCE_MODULE = "CHEMICAL"


class StockVerificationError(ValueError):
    """Operator-facing problem scheduling or reconciling a verification."""


async def _audit_type(db: AsyncSession) -> CamsAuditType:
    at = (
        await db.execute(
            select(CamsAuditType)
            .where(CamsAuditType.typeCode == AUDIT_TYPE_CODE)
            .where(CamsAuditType.isDeleted.is_(False))
        )
    ).scalar_one_or_none()
    if at is None:
        raise StockVerificationError(
            f"CAMS audit type '{AUDIT_TYPE_CODE}' is not configured. "
            f"Run `npx tsx prisma/apply-chemical-ddl.ts` to seed it — this module "
            f"deliberately does not create its own audit engine."
        )
    return at


async def _next_code(db: AsyncSession, plant_id: str) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"CSV-{year}-{plant_id[:6].upper()}-"
    existing = (
        await db.execute(
            select(CamsEngagement.engagementCode).where(CamsEngagement.engagementCode.like(f"{prefix}%"))
        )
    ).scalars().all()
    n = 0
    for c in existing:
        try:
            n = max(n, int(c.rsplit("-", 1)[-1]))
        except ValueError:
            continue
    return f"{prefix}{n + 1:03d}"


async def schedule_verification(
    db: AsyncSession,
    *,
    plant_id: str,
    lead_auditor_id: str,
    planned_date: datetime,
    storage_location_id: str | None = None,
    audit_team_ids: Sequence[str] = (),
    actor_id: str | None = None,
) -> CamsEngagement:
    """Create a stock-verification engagement in the CAMS engine."""
    at = await _audit_type(db)

    scope = "All chemical storage locations at this site."
    if storage_location_id:
        loc = await db.get(ChemicalStorageLocation, storage_location_id)
        if loc is None:
            raise StockVerificationError("Storage location not found.")
        scope = f"Physical stock count of {loc.name} ({loc.code})."

    eng = CamsEngagement(
        engagementCode=await _next_code(db, plant_id),
        title="Chemical stock verification",
        engagementType=at.engagementType,
        auditTypeId=at.id,
        standardRefs=list(at.standardRefs or []),
        siteId=plant_id,
        areaOrAssetRef=storage_location_id,
        scopeStatement=scope,
        leadAuditorId=lead_auditor_id,
        auditTeamIds=list(audit_team_ids),
        plannedDate=planned_date,
        status="PLANNED",
        templateId=at.defaultTemplateId,
        sourceModule=SOURCE_MODULE,
        sourceEntityId=storage_location_id,
        createdBy=actor_id,
    )
    db.add(eng)
    await db.flush()
    return eng


async def build_count_sheet(
    db: AsyncSession,
    *,
    tenant_id: str,
    plant_id: str,
    storage_location_id: str | None = None,
) -> list[dict[str, Any]]:
    """The batches an auditor is expected to count.

    `systemQuantity` is included so a discrepancy is computable, but note the
    ordering consequence: showing the expected figure invites confirmation bias
    in a physical count. The `/capture` PWA should collect the counted figure
    FIRST and only then reveal the system figure — that is a UI obligation this
    payload enables rather than enforces, and it is worth honouring.
    """
    stmt = (
        select(ChemicalInventoryItem, ChemicalMaster)
        .join(ChemicalMaster, ChemicalMaster.id == ChemicalInventoryItem.chemicalId)
        .where(ChemicalInventoryItem.tenantId == tenant_id)
        .where(ChemicalInventoryItem.plantId == plant_id)
        .where(ChemicalInventoryItem.isDeleted.is_(False))
        .where(ChemicalInventoryItem.quantityLedger > 0)
    )
    if storage_location_id:
        stmt = stmt.where(ChemicalInventoryItem.storageLocationId == storage_location_id)
    rows = (await db.execute(stmt.order_by(ChemicalMaster.name))).all()
    return [
        {
            "itemId": item.id,
            "chemicalId": chem.id,
            "chemicalName": chem.name,
            "casNumber": chem.casNumber,
            "hazardClasses": list(chem.hazardClasses or []),
            "batchLotNumber": item.batchLotNumber,
            "storageLocationId": item.storageLocationId,
            "unit": item.unit,
            "systemQuantity": item.quantityLedger,
            "expiryDate": item.expiryDate.isoformat() if item.expiryDate else None,
        }
        for item, chem in rows
    ]


async def reconcile_count(
    db: AsyncSession,
    *,
    tenant_id: str,
    engagement_id: str,
    counts: Sequence[dict[str, Any]],
    user_id: str,
) -> dict[str, Any]:
    """Apply a physical count as compensating ADJUSTMENT transactions.

    Never edits `quantityLedger` — it cannot, the database rejects it. A
    discrepancy becomes a signed ADJUSTMENT row citing the engagement, so the
    audit trail shows both what the system believed and what was found. That is
    the difference between a stock verification and a quiet correction.

    `counts` items: {itemId, countedQuantity, note?}
    """
    from app.services import chemical_ledger as ledger

    eng = await db.get(CamsEngagement, engagement_id)
    if eng is None:
        raise StockVerificationError("Verification engagement not found.")

    adjustments: list[dict[str, Any]] = []
    matched = 0
    for c in counts:
        item = await db.get(ChemicalInventoryItem, c["itemId"])
        if item is None or item.isDeleted:
            continue
        counted = float(c["countedQuantity"])
        delta = counted - float(item.quantityLedger)
        if abs(delta) < 1e-9:
            matched += 1
            continue
        await ledger.post_transaction(
            db,
            tenant_id=tenant_id,
            item_id=item.id,
            txn_type="ADJUSTMENT",
            quantity=abs(delta),
            unit=item.unit,
            user_id=user_id,
            adjustment_sign=1 if delta > 0 else -1,
            ref_document=eng.engagementCode,
            reason=(
                f"Stock verification {eng.engagementCode}: counted {counted} {item.unit}, "
                f"system held {item.quantityLedger} {item.unit}."
                + (f" {c['note']}" if c.get("note") else "")
            ),
            # A count correction can itself push a site over a threshold — that
            # is a real regulatory fact, not an artefact, so it must be evaluated.
            evaluate_thresholds_now=True,
        )
        adjustments.append({
            "itemId": item.id,
            "batchLotNumber": item.batchLotNumber,
            "systemQuantity": float(item.quantityLedger) - delta,
            "countedQuantity": counted,
            "delta": delta,
            "unit": item.unit,
        })

    await db.flush()
    logger.info(
        "[chemical_stock_verification] %s: %d matched, %d adjusted",
        eng.engagementCode, matched, len(adjustments),
    )
    return {
        "engagementCode": eng.engagementCode,
        "matched": matched,
        "adjusted": len(adjustments),
        "adjustments": adjustments,
    }


__all__ = [
    "AUDIT_TYPE_CODE",
    "StockVerificationError",
    "schedule_verification",
    "build_count_sheet",
    "reconcile_count",
]
