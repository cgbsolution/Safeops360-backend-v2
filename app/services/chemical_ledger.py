"""Inventory ledger service — the only writable path to chemical quantity.

Business rule §5: quantity is always the sum of transactions, never a directly
editable field. The database enforces that (a BEFORE UPDATE trigger rejects any
statement touching `quantityLedger`); this service is the sanctioned way to
change it, and it is where the domain consequences of a movement live:

  * RECEIPT / TRANSFER re-evaluate regulatory thresholds and can raise an MOC
    (§4.3). ISSUE and DISPOSAL only ever reduce site inventory, so they cannot
    cause a breach — they *can* clear one, which `evaluate_thresholds` handles
    on the next sweep and which the disposal path triggers explicitly.
  * Assigning an item to a storage location runs the co-storage check (§4.4).
  * DISPOSAL writes a DisposalRecord and feeds the EAI Register (§4.7).

Everything here participates in the caller's transaction and does not commit.
That matters most on the threshold path: the MOC and the receipt that justified
it must land together or not at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chemical import (
    ChemicalDisposalRecord,
    ChemicalInventoryItem,
    ChemicalInventoryTransaction,
    ChemicalMaster,
    ChemicalStorageLocation,
    ChemicalStorageOverride,
)
from app.services import chemical_incompatibility as incompat
from app.services.chemical_threshold import ThresholdEvaluation, evaluate_and_trigger
from app.services.trigger_engine import TriggerRun

logger = logging.getLogger(__name__)

#: Transactions that can only ever increase site inventory, and therefore the
#: only ones that can cause a threshold breach.
_INBOUND = {"RECEIPT", "TRANSFER_IN"}
_OUTBOUND = {"ISSUE", "TRANSFER_OUT", "DISPOSAL"}


class LedgerError(ValueError):
    """A movement that must not be recorded (insufficient stock, inactive
    chemical, blocked co-storage). Surfaces as a 4xx, never a 500 — these are
    operator errors with an actionable message, not system faults."""


@dataclass
class PostResult:
    transaction: ChemicalInventoryTransaction
    item: ChemicalInventoryItem
    evaluations: list[ThresholdEvaluation]
    trigger_run: TriggerRun | None
    warnings: list[str]


def _signed(txn_type: str, quantity: float, adjustment_sign: int = 1) -> float:
    if txn_type in _INBOUND:
        return quantity
    if txn_type in _OUTBOUND:
        return -quantity
    if txn_type == "ADJUSTMENT":
        return quantity * (1 if adjustment_sign >= 0 else -1)
    raise LedgerError(f"Unknown transaction type '{txn_type}'.")


async def _assert_chemical_usable(db: AsyncSession, chemical_id: str, txn_type: str) -> ChemicalMaster:
    chem = await db.get(ChemicalMaster, chemical_id)
    if chem is None or chem.isDeleted:
        raise LedgerError("Chemical not found.")
    # Receiving stock of a chemical that has not cleared review is the moment
    # the control is worth anything. Issuing or disposing of stock already on
    # site stays allowed for any status — refusing to let people get rid of a
    # RESTRICTED chemical would be an own goal.
    if txn_type in _INBOUND:
        if chem.status == "PENDING_SDS":
            raise LedgerError(
                f"'{chem.name}' is still PENDING_SDS. Attach and confirm the Safety Data "
                f"Sheet and have it approved before receiving stock."
            )
        if chem.status == "INACTIVE":
            raise LedgerError(f"'{chem.name}' is INACTIVE and cannot be received.")
        if chem.status == "RESTRICTED":
            raise LedgerError(
                f"'{chem.name}' is RESTRICTED"
                + (f": {chem.restrictionReason}" if chem.restrictionReason else "")
                + ". Receipt requires an HSE Manager exception."
            )
    return chem


async def post_transaction(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_id: str,
    txn_type: str,
    quantity: float,
    unit: str,
    user_id: str,
    transacted_at: datetime | None = None,
    ref_document: str | None = None,
    reason: str | None = None,
    counterpart_item_id: str | None = None,
    adjustment_sign: int = 1,
    region: str = "IN",
    evaluate_thresholds_now: bool = True,
) -> PostResult:
    """Append one ledger row and run its downstream consequences."""
    if quantity <= 0:
        raise LedgerError("Quantity must be greater than zero.")

    item = await db.get(ChemicalInventoryItem, item_id)
    if item is None or item.isDeleted:
        raise LedgerError("Inventory item not found.")
    if unit.strip().upper() != (item.unit or "").strip().upper():
        # Silently converting here would require densities this module does not
        # hold. Rejecting is the honest option.
        raise LedgerError(
            f"Transaction unit '{unit}' does not match the batch's unit '{item.unit}'. "
            f"Record the movement in the batch's own unit."
        )

    await _assert_chemical_usable(db, item.chemicalId, txn_type)

    signed = _signed(txn_type, quantity, adjustment_sign)
    if signed < 0 and (item.quantityLedger + signed) < 0:
        raise LedgerError(
            f"Cannot {txn_type.lower()} {quantity} {unit}: batch {item.batchLotNumber} "
            f"holds {item.quantityLedger} {item.unit}. Record a stock-verification "
            f"adjustment first if the physical count differs."
        )

    txn = ChemicalInventoryTransaction(
        tenantId=tenant_id,
        itemId=item.id,
        type=txn_type,
        quantity=quantity,
        signedQuantity=signed,
        unit=item.unit,
        transactedAt=transacted_at or datetime.now(timezone.utc),
        byUserId=user_id,
        refDocument=ref_document,
        reason=reason,
        counterpartItemId=counterpart_item_id,
    )
    db.add(txn)
    # The AFTER trigger recomputes quantityLedger/currentStatus on flush; the
    # refresh pulls the new values back into the session so the threshold
    # evaluation below sums current data rather than the pre-flush snapshot.
    await db.flush()
    await db.refresh(item)

    # Hazmat handling training (§4.8). Emitted for the person who moved the
    # stock — physical handling is what creates the exposure, so ISSUE and
    # DISPOSAL count as much as RECEIPT. Best-effort and non-blocking: a missing
    # competency mapping must not stop a store manager recording a movement, but
    # `chemical_training` logs the gap rather than swallowing it.
    try:
        from app.services import chemical_training

        await chemical_training.trigger_for_inventory_movement(
            db,
            item=item,
            person_user_ids=[user_id],
            activity=txn_type,
            source_record_id=txn.id,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[chemical_ledger] hazmat training trigger failed for transaction %s "
            "(movement recorded)", txn.id,
        )

    evaluations: list[ThresholdEvaluation] = []
    run: TriggerRun | None = None
    if evaluate_thresholds_now and txn_type in _INBOUND:
        evaluations, run = await evaluate_and_trigger(
            db,
            tenant_id=tenant_id,
            plant_id=item.plantId,
            chemical_id=item.chemicalId,
            actor_user_id=user_id,
            trigger_type="THRESHOLD_BREACH",
            region=region,
        )

    warnings: list[str] = []
    if run and run.failed:
        warnings.append(
            f"{len(run.failed)} automatic MOC trigger(s) failed and were logged; "
            f"the HSE Manager has been notified."
        )
    return PostResult(transaction=txn, item=item, evaluations=evaluations, trigger_run=run, warnings=warnings)


# ── storage assignment (§4.4) ─────────────────────────────────────────────────
async def assign_storage_location(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_id: str,
    storage_location_id: str,
    user_id: str,
    override_reason: str | None = None,
) -> list[incompat.CoStorageConflict]:
    """Move a batch into a storage location, enforcing the co-storage matrix.

    BLOCK conflicts raise `LedgerError` — and would still be rejected by the
    deferred database constraint if this check were bypassed. WARN conflicts
    require `override_reason`; the override is recorded for review rather than
    dismissed with a toast, mirroring the CAMS independence-waiver pattern.

    Returns the WARN conflicts that were overridden, so the caller can echo them
    back to the operator.
    """
    item = await db.get(ChemicalInventoryItem, item_id)
    if item is None or item.isDeleted:
        raise LedgerError("Inventory item not found.")
    loc = await db.get(ChemicalStorageLocation, storage_location_id)
    if loc is None or loc.isDeleted or not loc.isActive:
        raise LedgerError("Storage location not found or inactive.")
    if loc.plantId != item.plantId:
        raise LedgerError(
            "Storage location belongs to a different site. Use a TRANSFER instead of a re-assignment."
        )

    conflicts = await incompat.check_co_storage(
        db,
        tenant_id=tenant_id,
        storage_location_id=storage_location_id,
        chemical_id=item.chemicalId,
        exclude_item_id=item.id,
    )
    blocks = incompat.blocking(conflicts)
    if blocks:
        raise LedgerError(
            "Incompatible co-storage: "
            + " ".join(c.message() for c in blocks[:3])
            + " Assign a different storage location."
        )

    warns = incompat.warnings(conflicts)
    if warns and not (override_reason or "").strip():
        raise LedgerError(
            "Co-storage warning requires a documented reason: "
            + " ".join(c.message() for c in warns[:3])
        )

    item.storageLocationId = storage_location_id
    item.updatedBy = user_id
    await db.flush()

    from app.services.events import emit

    for c in warns:
        override = ChemicalStorageOverride(
            tenantId=tenant_id,
            plantId=item.plantId,
            storageLocationId=storage_location_id,
            inventoryItemId=item.id,
            conflictingItemId=c.other_item_id,
            ruleId=c.rule_id,
            severity="WARN",
            overrideReason=override_reason or "",
            overriddenByUserId=user_id,
        )
        db.add(override)
        await db.flush()
        # Daily Brief card: an accepted risk with no reviewer is a decision
        # nobody owns.
        emit(
            db,
            event_type="chemical.storage_override",
            entity_type="ChemicalStorageOverride",
            entity_id=override.id,
            site_id=item.plantId,
            actor_id=user_id,
            payload={
                "chemicalName": c.this_chemical_name,
                "conflictingChemicalName": c.other_chemical_name,
                "storageLocationName": loc.name,
                "overrideReason": override_reason,
                "regulatoryReference": c.regulatory_reference,
            },
        )
    await db.flush()
    return warns


# ── disposal (§4.7) ───────────────────────────────────────────────────────────
async def record_disposal(
    db: AsyncSession,
    *,
    tenant_id: str,
    item_id: str,
    quantity: float,
    disposal_date: datetime,
    manifest_reference: str,
    disposal_vendor: str,
    user_id: str,
    waste_category: str | None = None,
    disposal_method: str | None = None,
    vendor_authorisation_no: str | None = None,
    manifest_attachment_id: str | None = None,
    region: str = "IN",
) -> tuple[ChemicalDisposalRecord, PostResult]:
    """Disposal transaction + DisposalRecord + EAI Register linkage.

    Manifest reference and vendor are required by the workflow *and* by a CHECK
    constraint — a disposal without them is precisely the record a Pollution
    Control Board inspection asks for and the one that cannot be produced after
    the fact.
    """
    if not (manifest_reference or "").strip():
        raise LedgerError("A manifest reference is required to record a disposal.")
    if not (disposal_vendor or "").strip():
        raise LedgerError("An authorised disposal vendor is required to record a disposal.")

    item = await db.get(ChemicalInventoryItem, item_id)
    if item is None or item.isDeleted:
        raise LedgerError("Inventory item not found.")

    result = await post_transaction(
        db,
        tenant_id=tenant_id,
        item_id=item_id,
        txn_type="DISPOSAL",
        quantity=quantity,
        unit=item.unit,
        user_id=user_id,
        transacted_at=disposal_date,
        ref_document=manifest_reference,
        reason=f"Disposal via {disposal_vendor}",
        region=region,
    )

    record = ChemicalDisposalRecord(
        tenantId=tenant_id,
        plantId=item.plantId,
        inventoryItemId=item.id,
        chemicalId=item.chemicalId,
        quantity=quantity,
        unit=item.unit,
        disposalDate=disposal_date,
        manifestReference=manifest_reference.strip(),
        disposalVendor=disposal_vendor.strip(),
        vendorAuthorisationNo=vendor_authorisation_no,
        wasteCategory=waste_category,
        disposalMethod=disposal_method,
        manifestAttachmentId=manifest_attachment_id,
        recordedByUserId=user_id,
    )
    db.add(record)
    await db.flush()

    result.transaction.disposalRecordId = record.id

    # A disposal can take a site back below a threshold. Re-evaluating here is
    # what lets the breach episode close and the next genuine breach raise a
    # fresh MOC — without it, `activeMocId` would pin the site permanently.
    from app.services.chemical_threshold import evaluate_thresholds

    await evaluate_thresholds(
        db, tenant_id=tenant_id, plant_id=item.plantId, chemical_id=item.chemicalId, region=region
    )

    # EAI Register linkage is best-effort: the disposal record is the statutory
    # artefact and must not be lost because the environmental module is disabled
    # for this tenant. A failure is logged, and `eaiEntryId` stays null, which is
    # a visible gap rather than a silent one.
    try:
        from app.services.chemical_eai import link_disposal_to_eai

        entry_id = await link_disposal_to_eai(db, record)
        if entry_id:
            record.eaiEntryId = entry_id
            await db.flush()
    except Exception:  # noqa: BLE001
        logger.exception(
            "[chemical_ledger] EAI linkage failed for disposal %s (record kept)", record.id
        )

    return record, result


# ── transfer between sites/locations ──────────────────────────────────────────
async def transfer(
    db: AsyncSession,
    *,
    tenant_id: str,
    from_item_id: str,
    to_plant_id: str,
    to_storage_location_id: str | None,
    quantity: float,
    user_id: str,
    ref_document: str | None = None,
    region: str = "IN",
) -> tuple[PostResult, PostResult]:
    """A transfer is two ledger rows, not a moved row.

    Both ends stay reconcilable — the source shows what left, the destination
    shows what arrived, and neither can be edited afterwards. A transfer
    implemented as an UPDATE to `plantId` would erase the fact that the stock
    was ever at the origin, which is the fact a regulator asks about.
    """
    src = await db.get(ChemicalInventoryItem, from_item_id)
    if src is None or src.isDeleted:
        raise LedgerError("Source inventory item not found.")

    dest = (
        await db.execute(
            select(ChemicalInventoryItem)
            .where(ChemicalInventoryItem.tenantId == tenant_id)
            .where(ChemicalInventoryItem.chemicalId == src.chemicalId)
            .where(ChemicalInventoryItem.plantId == to_plant_id)
            .where(ChemicalInventoryItem.batchLotNumber == src.batchLotNumber)
            .where(ChemicalInventoryItem.isDeleted.is_(False))
        )
    ).scalar_one_or_none()
    if dest is None:
        dest = ChemicalInventoryItem(
            tenantId=tenant_id,
            chemicalId=src.chemicalId,
            plantId=to_plant_id,
            storageLocationId=None,  # assigned below, through the co-storage check
            batchLotNumber=src.batchLotNumber,
            unit=src.unit,
            receiptDate=datetime.now(timezone.utc),
            expiryDate=src.expiryDate,
            supplierName=src.supplierName,
            supplierBatchRef=src.supplierBatchRef,
            lowStockThreshold=src.lowStockThreshold,
            createdBy=user_id,
        )
        db.add(dest)
        await db.flush()

    out = await post_transaction(
        db, tenant_id=tenant_id, item_id=src.id, txn_type="TRANSFER_OUT",
        quantity=quantity, unit=src.unit, user_id=user_id,
        ref_document=ref_document, counterpart_item_id=dest.id, region=region,
    )
    into = await post_transaction(
        db, tenant_id=tenant_id, item_id=dest.id, txn_type="TRANSFER_IN",
        quantity=quantity, unit=src.unit, user_id=user_id,
        ref_document=ref_document, counterpart_item_id=src.id, region=region,
    )
    out.transaction.counterpartItemId = dest.id
    into.transaction.counterpartItemId = src.id

    if to_storage_location_id:
        # Runs the incompatibility check. A BLOCK here raises after the ledger
        # rows exist — correct, because the caller's transaction rolls the whole
        # transfer back, and a half-completed transfer is the one outcome that
        # must not be possible.
        await assign_storage_location(
            db, tenant_id=tenant_id, item_id=dest.id,
            storage_location_id=to_storage_location_id, user_id=user_id,
        )

    # The origin site may now be back under a threshold.
    from app.services.chemical_threshold import evaluate_thresholds

    await evaluate_thresholds(
        db, tenant_id=tenant_id, plant_id=src.plantId, chemical_id=src.chemicalId, region=region
    )
    return out, into


__all__ = [
    "LedgerError",
    "PostResult",
    "post_transaction",
    "assign_storage_location",
    "record_disposal",
    "transfer",
]
