"""Chemical / Hazmat Management API.

Chemical master data, the site inventory ledger, storage assignment with
co-storage enforcement, regulatory-threshold tracking with the auto-MOC trigger,
disposal records and the MOC trigger log.

RBAC uses the existing HSE codes (INCIDENT.READ / INCIDENT.UPDATE) plus
ADMIN.MANAGE for the config masters, matching how fire_safety.py bootstrapped
before dedicated grants were seeded. Swap the two constants below for CHEMICAL.*
codes once a licence including them is issued — every endpoint reads them, so it
is a two-line change rather than an audit.

Error-handling convention: `LedgerError` and `SdsError` are operator errors with
an actionable message and become 400/409. Anything else propagates to the app's
handler and becomes a logged 500 — this router never converts an unexpected
failure into an empty success, which is the defect class the whole build brief
is about.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.chemical import (
    HAZARD_CLASSES,
    ChemicalDisposalRecord,
    ChemicalIncompatibilityRule,
    ChemicalInventoryItem,
    ChemicalInventoryTransaction,
    ChemicalMaster,
    ChemicalStorageLocation,
    ChemicalStorageOverride,
    ChemicalThresholdRule,
    ChemicalThresholdState,
    MocTriggerLog,
)
from app.models.user import User
from app.services import chemical_hira
from app.services import chemical_incompatibility as incompat
from app.services import chemical_ledger as ledger
from app.services import chemical_sds as sds
from app.services import chemical_stock_verification as stockverify
from app.services.access_scope import build_query_scope
from app.services.chemical_threshold import evaluate_thresholds
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/chemicals", tags=["chemicals"])

_READ = "INCIDENT.READ"
_WRITE = "INCIDENT.UPDATE"
# Config masters (ThresholdRule, IncompatibilityMatrix, region mapping) are
# Admin-only per the build spec's role table. `CONFIGURATION.MASTERS` is the
# platform's existing code for master-data configuration and is held by ADMIN /
# SYSTEM_ADMIN only — an invented code like "ADMIN.MANAGE" would not exist in
# the permission catalogue, and `can()` fails closed on an unknown code, so
# those endpoints would have 403'd for everyone including administrators.
_ADMIN = "CONFIGURATION.MASTERS"


def _tenant(user: User) -> str:
    """Tenant key. The platform's models default to 'default' and the User table
    carries no tenant column yet; this is the single place that changes when it
    does, rather than 30 call sites."""
    return getattr(user, "tenantId", None) or "default"


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


# ── serialisers ───────────────────────────────────────────────────────────────
def _chem(c: ChemicalMaster) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "commonName": c.commonName,
        "casNumber": c.casNumber,
        "unNumber": c.unNumber,
        "hazardClasses": list(c.hazardClasses or []),
        "physicalState": c.physicalState,
        "flashPointCelsius": c.flashPointCelsius,
        "boilingPointCelsius": c.boilingPointCelsius,
        "nfpa": {
            "health": c.nfpaHealth,
            "flammability": c.nfpaFlammability,
            "reactivity": c.nfpaReactivity,
            "special": c.nfpaSpecial,
        },
        "hazardClassificationSource": c.hazardClassificationSource,
        "sdsAttachmentId": c.sdsAttachmentId,
        "sdsRevisionDate": _iso(c.sdsRevisionDate),
        "sdsReviewDueDate": _iso(c.sdsReviewDueDate),
        "sdsReviewOverdue": c.sdsReviewOverdue,
        "status": c.status,
        "restrictionReason": c.restrictionReason,
        "regulatoryReference": c.regulatoryReference,
        "approvedAt": _iso(c.approvedAt),
    }


def _item(i: ChemicalInventoryItem) -> dict[str, Any]:
    return {
        "id": i.id,
        "chemicalId": i.chemicalId,
        "chemicalName": i.chemical.name if i.chemical else None,
        "hazardClasses": list(i.chemical.hazardClasses or []) if i.chemical else [],
        "plantId": i.plantId,
        "storageLocationId": i.storageLocationId,
        "storageLocationName": i.storageLocation.name if i.storageLocation else None,
        "batchLotNumber": i.batchLotNumber,
        # Named `quantity` for the UI, sourced from the ledger-derived column.
        # There is no writable counterpart anywhere in this API — see §5.
        "quantity": i.quantityLedger,
        "unit": i.unit,
        "currentStatus": i.currentStatus,
        "lowStockThreshold": i.lowStockThreshold,
        "receiptDate": _iso(i.receiptDate),
        "expiryDate": _iso(i.expiryDate),
        "supplierName": i.supplierName,
    }


def _txn(t: ChemicalInventoryTransaction) -> dict[str, Any]:
    return {
        "id": t.id,
        "itemId": t.itemId,
        "type": t.type,
        "quantity": t.quantity,
        "signedQuantity": t.signedQuantity,
        "unit": t.unit,
        "transactedAt": _iso(t.transactedAt),
        "byUserId": t.byUserId,
        "refDocument": t.refDocument,
        "reason": t.reason,
        "counterpartItemId": t.counterpartItemId,
        "disposalRecordId": t.disposalRecordId,
    }


def _trigger_log(r: MocTriggerLog) -> dict[str, Any]:
    return {
        "id": r.id,
        "triggeredAt": _iso(r.triggeredAt),
        "triggerType": r.triggerType,
        "plantId": r.plantId,
        "sourceEntityType": r.sourceEntityType,
        "sourceEntityId": r.sourceEntityId,
        "mocId": r.mocId,
        "mocNumber": r.mocNumber,
        "status": r.status,
        "reason": r.reason,
        "failureReason": r.failureReason,
        "scheduleReference": r.scheduleReference,
        "observedQuantity": r.observedQuantity,
        "thresholdQuantity": r.thresholdQuantity,
        "unit": r.unit,
        "acknowledgedByUserId": r.acknowledgedByUserId,
        "acknowledgedAt": _iso(r.acknowledgedAt),
    }


# ── payloads ──────────────────────────────────────────────────────────────────
class ChemicalCreate(BaseModel):
    name: str
    commonName: str | None = None
    casNumber: str | None = None
    unNumber: str | None = None
    hazardClasses: list[str] = Field(default_factory=list)
    physicalState: Literal["SOLID", "LIQUID", "GAS"] = "LIQUID"
    flashPointCelsius: float | None = None
    boilingPointCelsius: float | None = None
    nfpaHealth: int | None = Field(default=None, ge=0, le=4)
    nfpaFlammability: int | None = Field(default=None, ge=0, le=4)
    nfpaReactivity: int | None = Field(default=None, ge=0, le=4)
    nfpaSpecial: str | None = None
    regulatoryReference: str | None = None


class ChemicalUpdate(ChemicalCreate):
    name: str | None = None  # type: ignore[assignment]


class SdsAttachPayload(BaseModel):
    attachmentId: str
    revisionDate: datetime
    validityYears: int = Field(default=3, ge=1, le=10)


class StatusPayload(BaseModel):
    status: Literal["PENDING_SDS", "ACTIVE", "INACTIVE", "RESTRICTED"]
    reason: str | None = None


class StorageLocationCreate(BaseModel):
    plantId: str
    zoneId: str | None = None
    code: str
    name: str
    storageType: Literal[
        "FLAMMABLE_CABINET", "VENTILATED_STORE", "COLD_STORE", "GENERAL", "OUTDOOR_BUND"
    ] = "GENERAL"
    maxCapacity: float | None = None
    capacityUnit: str | None = None
    ventilated: bool = False
    bunded: bool = False
    temperatureControlled: bool = False


class InventoryItemCreate(BaseModel):
    chemicalId: str
    plantId: str
    batchLotNumber: str
    unit: str = "KG"
    storageLocationId: str | None = None
    receiptDate: datetime | None = None
    expiryDate: datetime | None = None
    supplierName: str | None = None
    supplierBatchRef: str | None = None
    lowStockThreshold: float | None = None
    #: Opening quantity, posted as a RECEIPT ledger row — never written to the
    #: item directly. Omit for a batch created empty.
    openingQuantity: float | None = Field(default=None, gt=0)
    storageOverrideReason: str | None = None


class TransactionCreate(BaseModel):
    type: Literal["RECEIPT", "ISSUE", "DISPOSAL", "ADJUSTMENT"]
    quantity: float = Field(gt=0)
    unit: str
    transactedAt: datetime | None = None
    refDocument: str | None = None
    reason: str | None = None
    #: ADJUSTMENT only: -1 writes down, +1 writes up.
    adjustmentSign: int = 1


class TransferPayload(BaseModel):
    toPlantId: str
    toStorageLocationId: str | None = None
    quantity: float = Field(gt=0)
    refDocument: str | None = None


class AssignStoragePayload(BaseModel):
    storageLocationId: str
    overrideReason: str | None = None


class DisposalPayload(BaseModel):
    quantity: float = Field(gt=0)
    disposalDate: datetime
    manifestReference: str
    disposalVendor: str
    vendorAuthorisationNo: str | None = None
    wasteCategory: str | None = None
    disposalMethod: str | None = None
    manifestAttachmentId: str | None = None


class ThresholdRuleCreate(BaseModel):
    region: str = "IN"
    hazardClass: str | None = None
    chemicalId: str | None = None
    scheduleReference: str
    thresholdQuantity: float = Field(gt=0)
    unit: str = "KG"
    approachRatio: float = Field(default=0.8, gt=0, le=1)
    triggerObligation: Literal[
        "ON_SITE_EMERGENCY_PLAN", "OFF_SITE_EMERGENCY_PLAN", "SAFETY_REPORT", "LICENSE_UPGRADE"
    ]
    autoMocOnBreach: bool = True
    notes: str | None = None


class IncompatibilityCreate(BaseModel):
    hazardClassA: str | None = None
    hazardClassB: str | None = None
    chemicalIdA: str | None = None
    chemicalIdB: str | None = None
    severity: Literal["BLOCK", "WARN"] = "WARN"
    regulatoryReference: str | None = None
    rationale: str | None = None


# ═══ Chemical master (§7 #1, #2) ══════════════════════════════════════════════
@router.get("/masters")
async def list_chemicals(
    q: str | None = None,
    hazardClass: str | None = None,
    chemicalStatus: str | None = Query(default=None, alias="status"),
    sdsOverdue: bool | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    tenant = _tenant(user)
    stmt = (
        select(ChemicalMaster)
        .where(ChemicalMaster.tenantId == tenant)
        .where(ChemicalMaster.isDeleted.is_(False))
    )
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(ChemicalMaster.name).like(like),
                func.lower(ChemicalMaster.commonName).like(like),
                func.lower(ChemicalMaster.casNumber).like(like),
                func.lower(ChemicalMaster.unNumber).like(like),
            )
        )
    if hazardClass:
        stmt = stmt.where(ChemicalMaster.hazardClasses.contains([hazardClass]))
    if chemicalStatus:
        stmt = stmt.where(ChemicalMaster.status == chemicalStatus)
    if sdsOverdue is not None:
        stmt = stmt.where(ChemicalMaster.sdsReviewOverdue.is_(sdsOverdue))

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0
    rows = (
        await db.execute(stmt.order_by(ChemicalMaster.name).limit(limit).offset(offset))
    ).scalars().all()
    return {"total": int(total), "items": [_chem(c) for c in rows], "hazardClasses": list(HAZARD_CLASSES)}


@router.post("/masters", status_code=status.HTTP_201_CREATED)
async def create_chemical(
    payload: ChemicalCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _WRITE)
    unknown = [h for h in payload.hazardClasses if h not in HAZARD_CLASSES]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown hazard class(es): {', '.join(unknown)}. Valid: {', '.join(HAZARD_CLASSES)}",
        )
    chem = ChemicalMaster(
        tenantId=_tenant(user),
        name=payload.name,
        commonName=payload.commonName,
        casNumber=payload.casNumber,
        unNumber=payload.unNumber,
        hazardClasses=payload.hazardClasses,
        physicalState=payload.physicalState,
        flashPointCelsius=payload.flashPointCelsius,
        boilingPointCelsius=payload.boilingPointCelsius,
        nfpaHealth=payload.nfpaHealth,
        nfpaFlammability=payload.nfpaFlammability,
        nfpaReactivity=payload.nfpaReactivity,
        nfpaSpecial=payload.nfpaSpecial,
        regulatoryReference=payload.regulatoryReference,
        # Human-entered from a reading of the SDS. Never EXTRACTED — the SDS is
        # evidence here, not a parsed data source (§0).
        hazardClassificationSource="MANUAL",
        status="PENDING_SDS",
        createdBy=user.id,
    )
    db.add(chem)
    await db.commit()
    await db.refresh(chem)
    return _chem(chem)


@router.get("/masters/{chemical_id}")
async def get_chemical(
    chemical_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    chem = await db.get(ChemicalMaster, chemical_id)
    if chem is None or chem.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chemical not found")

    scope = await build_query_scope(db, user.id, _READ)
    inv_stmt = (
        select(ChemicalInventoryItem)
        .where(ChemicalInventoryItem.chemicalId == chemical_id)
        .where(ChemicalInventoryItem.isDeleted.is_(False))
    )
    if not scope.all_plants:
        inv_stmt = inv_stmt.where(ChemicalInventoryItem.plantId.in_(scope.plant_ids or ["__none__"]))
    inventory = (await db.execute(inv_stmt)).scalars().all()

    return {
        **_chem(chem),
        "inventory": [_item(i) for i in inventory],
        "totalOnHand": sum(i.quantityLedger for i in inventory),
    }


@router.patch("/masters/{chemical_id}")
async def update_chemical(
    chemical_id: str,
    payload: ChemicalUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _WRITE)
    chem = await db.get(ChemicalMaster, chemical_id)
    if chem is None or chem.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chemical not found")
    data = payload.model_dump(exclude_unset=True)
    if "hazardClasses" in data:
        unknown = [h for h in data["hazardClasses"] if h not in HAZARD_CLASSES]
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Unknown hazard class(es): {', '.join(unknown)}"
            )
    for k, v in data.items():
        setattr(chem, k, v)
    chem.updatedBy = user.id
    await db.commit()
    await db.refresh(chem)
    return _chem(chem)


@router.post("/masters/{chemical_id}/sds")
async def attach_sds(
    chemical_id: str,
    payload: SdsAttachPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Link an already-uploaded SDS document (evidence layer, basic tier).

    The file itself is uploaded through `/api/attachments` with
    entityType=`chemical_master`, category=`SDS_SHEET`. Nothing here opens it.
    """
    await _require(db, user, _WRITE)
    try:
        chem = await sds.attach_sds(
            db,
            chemical_id=chemical_id,
            attachment_id=payload.attachmentId,
            revision_date=payload.revisionDate,
            user_id=user.id,
            validity_years=payload.validityYears,
        )
    except sds.SdsError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    await db.refresh(chem)
    return _chem(chem)


@router.post("/masters/{chemical_id}/status")
async def set_chemical_status(
    chemical_id: str,
    payload: StatusPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _WRITE)
    try:
        chem = await sds.set_status(
            db, chemical_id=chemical_id, status=payload.status, user_id=user.id, reason=payload.reason
        )
    except sds.SdsError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    await db.refresh(chem)
    return _chem(chem)


# ═══ Storage locations (§7 #4) ════════════════════════════════════════════════
@router.get("/storage-locations")
async def list_storage_locations(
    plantId: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require(db, user, _READ, plantId)
    rows = (
        await db.execute(
            select(ChemicalStorageLocation)
            .where(ChemicalStorageLocation.plantId == plantId)
            .where(ChemicalStorageLocation.isDeleted.is_(False))
            .order_by(ChemicalStorageLocation.code)
        )
    ).scalars().all()
    out = []
    for loc in rows:
        occupants = (
            await db.execute(
                select(ChemicalInventoryItem)
                .where(ChemicalInventoryItem.storageLocationId == loc.id)
                .where(ChemicalInventoryItem.isDeleted.is_(False))
                .where(ChemicalInventoryItem.quantityLedger > 0)
            )
        ).scalars().all()
        out.append({
            "id": loc.id,
            "plantId": loc.plantId,
            "zoneId": loc.zoneId,
            "code": loc.code,
            "name": loc.name,
            "storageType": loc.storageType,
            "maxCapacity": loc.maxCapacity,
            "capacityUnit": loc.capacityUnit,
            "currentOccupancy": loc.currentOccupancy,
            "ventilated": loc.ventilated,
            "bunded": loc.bunded,
            "temperatureControlled": loc.temperatureControlled,
            "itemCount": len(occupants),
            "items": [_item(i) for i in occupants],
        })
    return out


@router.post("/storage-locations", status_code=status.HTTP_201_CREATED)
async def create_storage_location(
    payload: StorageLocationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _WRITE, payload.plantId)
    loc = ChemicalStorageLocation(
        tenantId=_tenant(user), createdBy=user.id, **payload.model_dump()
    )
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return {"id": loc.id, "code": loc.code, "name": loc.name}


@router.get("/storage-locations/{location_id}/conflicts")
async def preview_conflicts(
    location_id: str,
    chemicalId: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Co-storage conflicts BEFORE anyone presses save (§7 #4 — surfaced
    visually, not just on save)."""
    await _require(db, user, _READ)
    conflicts = await incompat.check_co_storage(
        db, tenant_id=_tenant(user), storage_location_id=location_id, chemical_id=chemicalId
    )
    return {
        "blocked": bool(incompat.blocking(conflicts)),
        "conflicts": [
            {
                "severity": c.severity,
                "message": c.message(),
                "otherItemId": c.other_item_id,
                "otherChemicalName": c.other_chemical_name,
                "otherBatch": c.other_batch,
                "hazardPair": list(c.hazard_pair),
                "regulatoryReference": c.regulatory_reference,
                "rationale": c.rationale,
            }
            for c in conflicts
        ],
    }


# ═══ Inventory ledger (§7 #3) ═════════════════════════════════════════════════
@router.get("/inventory")
async def list_inventory(
    plantId: str,
    storageLocationId: str | None = None,
    chemicalId: str | None = None,
    itemStatus: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require(db, user, _READ, plantId)
    stmt = (
        select(ChemicalInventoryItem)
        .where(ChemicalInventoryItem.plantId == plantId)
        .where(ChemicalInventoryItem.isDeleted.is_(False))
    )
    if storageLocationId:
        stmt = stmt.where(ChemicalInventoryItem.storageLocationId == storageLocationId)
    if chemicalId:
        stmt = stmt.where(ChemicalInventoryItem.chemicalId == chemicalId)
    if itemStatus:
        stmt = stmt.where(ChemicalInventoryItem.currentStatus == itemStatus)
    rows = (
        await db.execute(stmt.order_by(ChemicalInventoryItem.batchLotNumber).limit(limit))
    ).scalars().all()
    return [_item(i) for i in rows]


@router.post("/inventory", status_code=status.HTTP_201_CREATED)
async def create_inventory_item(
    payload: InventoryItemCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a batch. Any opening quantity is posted as a RECEIPT ledger row —
    there is no path in this API that writes a quantity onto the item."""
    await _require(db, user, _WRITE, payload.plantId)
    tenant = _tenant(user)

    item = ChemicalInventoryItem(
        tenantId=tenant,
        chemicalId=payload.chemicalId,
        plantId=payload.plantId,
        batchLotNumber=payload.batchLotNumber,
        unit=payload.unit,
        receiptDate=payload.receiptDate or datetime.now(timezone.utc),
        expiryDate=payload.expiryDate,
        supplierName=payload.supplierName,
        supplierBatchRef=payload.supplierBatchRef,
        lowStockThreshold=payload.lowStockThreshold,
        createdBy=user.id,
    )
    db.add(item)
    await db.flush()

    warnings: list[str] = []
    trigger_summary: dict[str, Any] | None = None
    try:
        if payload.storageLocationId:
            warned = await ledger.assign_storage_location(
                db,
                tenant_id=tenant,
                item_id=item.id,
                storage_location_id=payload.storageLocationId,
                user_id=user.id,
                override_reason=payload.storageOverrideReason,
            )
            warnings += [c.message() for c in warned]
        if payload.openingQuantity:
            res = await ledger.post_transaction(
                db,
                tenant_id=tenant,
                item_id=item.id,
                txn_type="RECEIPT",
                quantity=payload.openingQuantity,
                unit=payload.unit,
                user_id=user.id,
                ref_document=payload.supplierBatchRef,
                reason="Opening receipt",
            )
            warnings += res.warnings
            trigger_summary = _summarise_run(res)
    except ledger.LedgerError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))

    await db.commit()
    await db.refresh(item)
    return {**_item(item), "warnings": warnings, "triggers": trigger_summary}


def _summarise_run(res: ledger.PostResult) -> dict[str, Any] | None:
    if res.trigger_run is None:
        return None
    return {
        "fired": [
            {"rule": r.rule_name, "reason": r.reason, "mocId": r.spawned_record_id}
            for r in res.trigger_run.fired
        ],
        "failed": [
            {"rule": r.rule_name, "failureReason": r.failure_reason}
            for r in res.trigger_run.failed
        ],
        "auditPersisted": not res.trigger_run.sink_failed,
    }


@router.get("/inventory/{item_id}/transactions")
async def list_transactions(
    item_id: str,
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require(db, user, _READ)
    rows = (
        await db.execute(
            select(ChemicalInventoryTransaction)
            .where(ChemicalInventoryTransaction.itemId == item_id)
            .order_by(ChemicalInventoryTransaction.transactedAt.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_txn(t) for t in rows]


@router.post("/inventory/{item_id}/transactions", status_code=status.HTTP_201_CREATED)
async def post_transaction(
    item_id: str,
    payload: TransactionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _WRITE)
    if payload.type == "DISPOSAL":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Use POST /inventory/{id}/disposal — a disposal requires a manifest reference "
            "and vendor, which this endpoint cannot capture.",
        )
    try:
        res = await ledger.post_transaction(
            db,
            tenant_id=_tenant(user),
            item_id=item_id,
            txn_type=payload.type,
            quantity=payload.quantity,
            unit=payload.unit,
            user_id=user.id,
            transacted_at=payload.transactedAt,
            ref_document=payload.refDocument,
            reason=payload.reason,
            adjustment_sign=payload.adjustmentSign,
        )
    except ledger.LedgerError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await db.commit()
    await db.refresh(res.item)
    return {
        "transaction": _txn(res.transaction),
        "item": _item(res.item),
        "warnings": res.warnings,
        "triggers": _summarise_run(res),
    }


@router.post("/inventory/{item_id}/storage")
async def assign_storage(
    item_id: str,
    payload: AssignStoragePayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _WRITE)
    try:
        warned = await ledger.assign_storage_location(
            db,
            tenant_id=_tenant(user),
            item_id=item_id,
            storage_location_id=payload.storageLocationId,
            user_id=user.id,
            override_reason=payload.overrideReason,
        )
    except ledger.LedgerError as e:
        await db.rollback()
        # 409, not 400: the request is well-formed, the current state forbids it.
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await db.commit()
    return {"ok": True, "overriddenWarnings": [c.message() for c in warned]}


@router.post("/inventory/{item_id}/transfer")
async def transfer_stock(
    item_id: str,
    payload: TransferPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _WRITE, payload.toPlantId)
    try:
        out, into = await ledger.transfer(
            db,
            tenant_id=_tenant(user),
            from_item_id=item_id,
            to_plant_id=payload.toPlantId,
            to_storage_location_id=payload.toStorageLocationId,
            quantity=payload.quantity,
            user_id=user.id,
            ref_document=payload.refDocument,
        )
    except ledger.LedgerError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await db.commit()
    return {
        "out": _txn(out.transaction),
        "in": _txn(into.transaction),
        "triggers": _summarise_run(into),
    }


@router.post("/inventory/{item_id}/disposal", status_code=status.HTTP_201_CREATED)
async def record_disposal(
    item_id: str,
    payload: DisposalPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _WRITE)
    try:
        record, res = await ledger.record_disposal(
            db,
            tenant_id=_tenant(user),
            item_id=item_id,
            quantity=payload.quantity,
            disposal_date=payload.disposalDate,
            manifest_reference=payload.manifestReference,
            disposal_vendor=payload.disposalVendor,
            user_id=user.id,
            waste_category=payload.wasteCategory,
            disposal_method=payload.disposalMethod,
            vendor_authorisation_no=payload.vendorAuthorisationNo,
            manifest_attachment_id=payload.manifestAttachmentId,
        )
    except ledger.LedgerError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await db.commit()
    await db.refresh(record)
    return {
        "id": record.id,
        "manifestReference": record.manifestReference,
        "disposalVendor": record.disposalVendor,
        "quantity": record.quantity,
        "unit": record.unit,
        "eaiEntryId": record.eaiEntryId,
        # Null eaiEntryId is a visible gap, not a silent one — the module does
        # not fabricate environmental risk scores to fill it. See chemical_eai.
        "eaiLinked": record.eaiEntryId is not None,
        "item": _item(res.item),
    }


# ═══ Disposal register (§7 #7) ════════════════════════════════════════════════
@router.get("/disposals")
async def list_disposals(
    plantId: str | None = None,
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require(db, user, _READ, plantId)
    stmt = (
        select(ChemicalDisposalRecord, ChemicalMaster)
        .join(ChemicalMaster, ChemicalMaster.id == ChemicalDisposalRecord.chemicalId)
        .where(ChemicalDisposalRecord.tenantId == _tenant(user))
        .where(ChemicalDisposalRecord.isDeleted.is_(False))
    )
    if plantId:
        stmt = stmt.where(ChemicalDisposalRecord.plantId == plantId)
    rows = (
        await db.execute(stmt.order_by(ChemicalDisposalRecord.disposalDate.desc()).limit(limit))
    ).all()
    return [
        {
            "id": d.id,
            "plantId": d.plantId,
            "chemicalId": d.chemicalId,
            "chemicalName": c.name,
            "quantity": d.quantity,
            "unit": d.unit,
            "disposalDate": _iso(d.disposalDate),
            "manifestReference": d.manifestReference,
            "disposalVendor": d.disposalVendor,
            "vendorAuthorisationNo": d.vendorAuthorisationNo,
            "wasteCategory": d.wasteCategory,
            "disposalMethod": d.disposalMethod,
            "manifestAttachmentId": d.manifestAttachmentId,
            "eaiEntryId": d.eaiEntryId,
        }
        for d, c in rows
    ]


# ═══ Threshold dashboard (§7 #5) ══════════════════════════════════════════════
@router.get("/thresholds/dashboard")
async def threshold_dashboard(
    plantId: str,
    region: str = "IN",
    recompute: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Site quantity vs threshold by hazard class, with obligation status.

    `recompute=true` re-evaluates from the ledger instead of reading the stored
    state. Off by default because the dashboard is read frequently and the state
    is maintained on every movement; on when someone needs to be sure.
    """
    await _require(db, user, _READ, plantId)
    tenant = _tenant(user)

    if recompute:
        evaluations = await evaluate_thresholds(
            db, tenant_id=tenant, plant_id=plantId, region=region
        )
        await db.commit()
        rows = [
            {
                "ruleId": e.rule.id,
                "scheduleReference": e.rule.scheduleReference,
                "hazardClass": e.rule.hazardClass,
                "chemicalId": e.rule.chemicalId,
                "triggerObligation": e.rule.triggerObligation,
                "autoMocOnBreach": e.rule.autoMocOnBreach,
                "currentQuantity": e.observed_quantity,
                "thresholdQuantity": e.threshold_quantity,
                "unit": e.unit,
                "status": e.status,
                "percentOfThreshold": (
                    round(100 * e.observed_quantity / e.threshold_quantity, 1)
                    if e.threshold_quantity else None
                ),
                "evaluationCaveat": e.skipped_reason,
                "contributors": e.contributing_chemicals,
            }
            for e in evaluations
        ]
    else:
        states = (
            await db.execute(
                select(ChemicalThresholdState, ChemicalThresholdRule)
                .join(ChemicalThresholdRule, ChemicalThresholdRule.id == ChemicalThresholdState.ruleId)
                .where(ChemicalThresholdState.tenantId == tenant)
                .where(ChemicalThresholdState.plantId == plantId)
            )
        ).all()
        rows = [
            {
                "ruleId": r.id,
                "scheduleReference": r.scheduleReference,
                "hazardClass": r.hazardClass,
                "chemicalId": r.chemicalId,
                "triggerObligation": r.triggerObligation,
                "autoMocOnBreach": r.autoMocOnBreach,
                "currentQuantity": s.currentQuantity,
                "thresholdQuantity": s.thresholdQuantity,
                "unit": s.unit,
                "status": s.status,
                "percentOfThreshold": (
                    round(100 * s.currentQuantity / s.thresholdQuantity, 1)
                    if s.thresholdQuantity else None
                ),
                "activeMocId": s.activeMocId,
                "lastEvaluatedAt": _iso(s.lastEvaluatedAt),
                "lastBreachedAt": _iso(s.lastBreachedAt),
            }
            for s, r in states
        ]

    rows.sort(key=lambda x: -(x.get("percentOfThreshold") or 0))
    return {
        "plantId": plantId,
        "breached": [r for r in rows if r["status"] == "BREACHED"],
        "approaching": [r for r in rows if r["status"] == "APPROACHING"],
        "rules": rows,
    }


@router.get("/thresholds/rules")
async def list_threshold_rules(
    region: str = "IN",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require(db, user, _READ)
    tenant = _tenant(user)
    rows = (
        await db.execute(
            select(ChemicalThresholdRule)
            .where(ChemicalThresholdRule.isDeleted.is_(False))
            .where(ChemicalThresholdRule.region == region)
            .where(
                or_(
                    ChemicalThresholdRule.tenantId.is_(None),
                    ChemicalThresholdRule.tenantId == tenant,
                )
            )
            .order_by(ChemicalThresholdRule.scheduleReference)
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "isPlatformDefault": r.tenantId is None,
            "region": r.region,
            "hazardClass": r.hazardClass,
            "chemicalId": r.chemicalId,
            "scheduleReference": r.scheduleReference,
            "thresholdQuantity": r.thresholdQuantity,
            "unit": r.unit,
            "approachRatio": r.approachRatio,
            "triggerObligation": r.triggerObligation,
            "autoMocOnBreach": r.autoMocOnBreach,
            "isActive": r.isActive,
            "notes": r.notes,
        }
        for r in rows
    ]


@router.post("/thresholds/rules", status_code=status.HTTP_201_CREATED)
async def create_threshold_rule(
    payload: ThresholdRuleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Admin-configured. Region + schedule + quantity are data, so a GCC
    regulatory remap is a set of rows, not a release (§5 rule 2)."""
    await _require(db, user, _ADMIN)
    if not payload.hazardClass and not payload.chemicalId:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A threshold rule must name either a hazard class or a specific chemical.",
        )
    rule = ChemicalThresholdRule(
        tenantId=_tenant(user), createdBy=user.id, **payload.model_dump()
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return {"id": rule.id, "scheduleReference": rule.scheduleReference}


# ═══ Incompatibility matrix ═══════════════════════════════════════════════════
@router.get("/incompatibility")
async def list_incompatibility(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require(db, user, _READ)
    tenant = _tenant(user)
    rows = (
        await db.execute(
            select(ChemicalIncompatibilityRule)
            .where(ChemicalIncompatibilityRule.isDeleted.is_(False))
            .where(
                or_(
                    ChemicalIncompatibilityRule.tenantId.is_(None),
                    ChemicalIncompatibilityRule.tenantId == tenant,
                )
            )
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "isPlatformDefault": r.tenantId is None,
            "hazardClassA": r.hazardClassA,
            "hazardClassB": r.hazardClassB,
            "chemicalIdA": r.chemicalIdA,
            "chemicalIdB": r.chemicalIdB,
            "severity": r.severity,
            "regulatoryReference": r.regulatoryReference,
            "rationale": r.rationale,
            "isActive": r.isActive,
        }
        for r in rows
    ]


@router.post("/incompatibility", status_code=status.HTTP_201_CREATED)
async def create_incompatibility(
    payload: IncompatibilityCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _ADMIN)
    has_classes = bool(payload.hazardClassA and payload.hazardClassB)
    has_chems = bool(payload.chemicalIdA and payload.chemicalIdB)
    if not (has_classes or has_chems):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A rule must name a hazard-class pair or a specific chemical pair.",
        )
    rule = ChemicalIncompatibilityRule(
        tenantId=_tenant(user), createdBy=user.id, **payload.model_dump()
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return {"id": rule.id, "severity": rule.severity}


@router.get("/storage-overrides")
async def list_storage_overrides(
    plantId: str | None = None,
    pendingOnly: bool = True,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """WARN-severity co-storage overrides. Pending ones are a Daily Brief card —
    an accepted risk nobody has reviewed is a decision without an owner."""
    await _require(db, user, _READ, plantId)
    stmt = select(ChemicalStorageOverride).where(
        ChemicalStorageOverride.tenantId == _tenant(user)
    )
    if plantId:
        stmt = stmt.where(ChemicalStorageOverride.plantId == plantId)
    if pendingOnly:
        stmt = stmt.where(ChemicalStorageOverride.reviewedAt.is_(None))
    rows = (
        await db.execute(stmt.order_by(ChemicalStorageOverride.overriddenAt.desc()).limit(200))
    ).scalars().all()
    return [
        {
            "id": o.id,
            "plantId": o.plantId,
            "storageLocationId": o.storageLocationId,
            "inventoryItemId": o.inventoryItemId,
            "conflictingItemId": o.conflictingItemId,
            "severity": o.severity,
            "overrideReason": o.overrideReason,
            "overriddenByUserId": o.overriddenByUserId,
            "overriddenAt": _iso(o.overriddenAt),
            "reviewedAt": _iso(o.reviewedAt),
            "reviewOutcome": o.reviewOutcome,
        }
        for o in rows
    ]


# ═══ MOC trigger log (§7 #6) ══════════════════════════════════════════════════
@router.get("/moc-trigger-log")
async def moc_trigger_log(
    plantId: str | None = None,
    logStatus: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The visible audit trail. Its existence in the UI is the point: a trigger
    whose outcome is only in a log file is a trigger nobody can verify."""
    await _require(db, user, _READ, plantId)
    stmt = select(MocTriggerLog).where(MocTriggerLog.tenantId == _tenant(user))
    if plantId:
        stmt = stmt.where(MocTriggerLog.plantId == plantId)
    if logStatus:
        stmt = stmt.where(MocTriggerLog.status == logStatus)
    rows = (
        await db.execute(stmt.order_by(MocTriggerLog.triggeredAt.desc()).limit(limit))
    ).scalars().all()

    counts_stmt = select(MocTriggerLog.status, func.count()).where(
        MocTriggerLog.tenantId == _tenant(user)
    )
    if plantId:
        counts_stmt = counts_stmt.where(MocTriggerLog.plantId == plantId)
    counts = dict((await db.execute(counts_stmt.group_by(MocTriggerLog.status))).all())

    return {
        "counts": {
            "FIRED": int(counts.get("FIRED", 0)),
            "FAILED": int(counts.get("FAILED", 0)),
            "SKIPPED": int(counts.get("SKIPPED", 0)),
        },
        "entries": [_trigger_log(r) for r in rows],
    }


@router.post("/moc-trigger-log/{log_id}/acknowledge")
async def acknowledge_trigger_failure(
    log_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Mark a FAILED trigger as picked up. Does not resolve it — the MOC still
    has to be raised by hand — but records that someone owns it."""
    await _require(db, user, _WRITE)
    row = await db.get(MocTriggerLog, log_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trigger log entry not found")
    row.acknowledgedByUserId = user.id
    row.acknowledgedAt = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "acknowledgedAt": _iso(row.acknowledgedAt)}


# ═══ Stock verification — CAMS engine reuse (§4.6) ════════════════════════════
class ScheduleVerificationPayload(BaseModel):
    plantId: str
    leadAuditorId: str
    plannedDate: datetime
    storageLocationId: str | None = None
    auditTeamIds: list[str] = Field(default_factory=list)


class CountLine(BaseModel):
    itemId: str
    countedQuantity: float = Field(ge=0)
    note: str | None = None


class ReconcilePayload(BaseModel):
    counts: list[CountLine]


@router.post("/stock-verification", status_code=status.HTTP_201_CREATED)
async def schedule_stock_verification(
    payload: ScheduleVerificationPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Schedule a physical stock count. Creates a CamsEngagement — there is no
    chemical-specific audit engine, by design (AC #5)."""
    await _require(db, user, _WRITE, payload.plantId)
    try:
        eng = await stockverify.schedule_verification(
            db,
            plant_id=payload.plantId,
            lead_auditor_id=payload.leadAuditorId,
            planned_date=payload.plannedDate,
            storage_location_id=payload.storageLocationId,
            audit_team_ids=payload.auditTeamIds,
            actor_id=user.id,
        )
    except stockverify.StockVerificationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await db.commit()
    return {
        "id": eng.id,
        "engagementCode": eng.engagementCode,
        "status": eng.status,
        "plannedDate": _iso(eng.plannedDate),
        # The engagement lives in CAMS; the UI links there rather than
        # reimplementing conduct screens.
        "conductUrl": f"/cams/engagements/{eng.id}",
    }


@router.get("/stock-verification/count-sheet")
async def stock_count_sheet(
    plantId: str,
    storageLocationId: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await _require(db, user, _READ, plantId)
    return await stockverify.build_count_sheet(
        db, tenant_id=_tenant(user), plant_id=plantId, storage_location_id=storageLocationId
    )


@router.post("/stock-verification/{engagement_id}/reconcile")
async def reconcile_stock_count(
    engagement_id: str,
    payload: ReconcilePayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Apply a count as ADJUSTMENT ledger rows. Discrepancies stay visible —
    nothing here edits a quantity."""
    await _require(db, user, _WRITE)
    try:
        result = await stockverify.reconcile_count(
            db,
            tenant_id=_tenant(user),
            engagement_id=engagement_id,
            counts=[c.model_dump() for c in payload.counts],
            user_id=user.id,
        )
    except (stockverify.StockVerificationError, ledger.LedgerError) as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await db.commit()
    return result


# ═══ HIRA hazard-row linkage (§4.8) ═══════════════════════════════════════════
@router.get("/masters/{chemical_id}/hira-hazards")
async def proposed_hira_hazards(
    chemical_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    await _require(db, user, _READ)
    chem = await db.get(ChemicalMaster, chemical_id)
    if chem is None or chem.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chemical not found")
    proposals, missing = await chemical_hira.resolve_hazards(db, chem)
    return {
        "proposals": [
            {
                "hazardId": p.hazard_id,
                "hazardCode": p.hazard_code,
                "hazardName": p.hazard_name,
                "sourceHazardClass": p.source_hazard_class,
                "contextualDescription": p.contextual_description,
                "regulationRef": p.regulation_ref,
                "regulationSection": p.regulation_section,
            }
            for p in proposals
        ],
        # Surfaced, never swallowed: a mapped library hazard that isn't seeded
        # would otherwise produce a quietly short HIRA.
        "missingLibraryHazards": missing,
    }


class ApplyHazardsPayload(BaseModel):
    entryId: str
    replace: bool = False


@router.post("/masters/{chemical_id}/hira-hazards")
async def apply_hira_hazards(
    chemical_id: str,
    payload: ApplyHazardsPayload,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Materialise hazard rows onto a HIRA entry that is being authored.
    Refuses an APPROVED entry — see chemical_hira.apply_to_entry."""
    await _require(db, user, _WRITE)
    try:
        result = await chemical_hira.apply_to_entry(
            db, entry_id=payload.entryId, chemical_id=chemical_id, replace=payload.replace
        )
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await db.commit()
    return result


# ═══ Command Centre widget (§7 #8) ════════════════════════════════════════════
@router.get("/dashboard")
async def chemical_dashboard(
    plantId: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Everything the Command Centre widget and the Daily Brief cards read."""
    await _require(db, user, _READ, plantId)
    tenant = _tenant(user)

    overdue = await sds.overdue_sds(db, tenant_id=tenant, limit=20)
    expiring = await sds.expiring_sds(db, tenant_id=tenant, within_days=60, limit=20)

    breach_stmt = (
        select(ChemicalThresholdState, ChemicalThresholdRule)
        .join(ChemicalThresholdRule, ChemicalThresholdRule.id == ChemicalThresholdState.ruleId)
        .where(ChemicalThresholdState.tenantId == tenant)
        .where(ChemicalThresholdState.status.in_(["BREACHED", "APPROACHING"]))
    )
    if plantId:
        breach_stmt = breach_stmt.where(ChemicalThresholdState.plantId == plantId)
    breaches = (await db.execute(breach_stmt)).all()

    failed_stmt = (
        select(MocTriggerLog)
        .where(MocTriggerLog.tenantId == tenant)
        .where(MocTriggerLog.status == "FAILED")
        .where(MocTriggerLog.acknowledgedAt.is_(None))
        .order_by(MocTriggerLog.triggeredAt.desc())
        .limit(20)
    )
    if plantId:
        failed_stmt = failed_stmt.where(MocTriggerLog.plantId == plantId)
    failed = (await db.execute(failed_stmt)).scalars().all()

    override_stmt = (
        select(func.count(ChemicalStorageOverride.id))
        .where(ChemicalStorageOverride.tenantId == tenant)
        .where(ChemicalStorageOverride.reviewedAt.is_(None))
    )
    if plantId:
        override_stmt = override_stmt.where(ChemicalStorageOverride.plantId == plantId)
    pending_overrides = int((await db.execute(override_stmt)).scalar() or 0)

    return {
        "sdsOverdue": {
            "count": len(overdue),
            "items": [{"id": c.id, "name": c.name, "dueDate": _iso(c.sdsReviewDueDate)} for c in overdue],
        },
        "sdsExpiringSoon": {
            "count": len(expiring),
            "items": [{"id": c.id, "name": c.name, "dueDate": _iso(c.sdsReviewDueDate)} for c in expiring],
        },
        "thresholds": {
            "breached": len([1 for s, _ in breaches if s.status == "BREACHED"]),
            "approaching": len([1 for s, _ in breaches if s.status == "APPROACHING"]),
            "items": [
                {
                    "plantId": s.plantId,
                    "scheduleReference": r.scheduleReference,
                    "hazardClass": r.hazardClass,
                    "status": s.status,
                    "currentQuantity": s.currentQuantity,
                    "thresholdQuantity": s.thresholdQuantity,
                    "unit": s.unit,
                    "activeMocId": s.activeMocId,
                }
                for s, r in breaches
            ],
        },
        "failedTriggers": {"count": len(failed), "items": [_trigger_log(r) for r in failed]},
        "pendingStorageOverrides": pending_overrides,
    }
