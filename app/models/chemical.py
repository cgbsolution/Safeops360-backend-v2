"""Chemical / Hazmat Management module — data model.

Site-level chemical inventory with a tamper-evident ledger, SDS review-cycle
tracking, co-storage incompatibility enforcement, and regulatory-threshold
tracking (MSIHC Schedules, PESO/Explosives licence categories) that raises an
MOC automatically when a site crosses a threshold.

Six modelling decisions here are load-bearing and are NOT free to change
without re-reading the business rules they implement:

 1. **No writable quantity column.** Business rule §5 says inventory quantity is
    always ledger-derived. `ChemicalInventoryItem.quantity` therefore does not
    exist as an editable field: `quantityLedger` is maintained *only* by a
    database trigger over `ChemicalInventoryTransaction`, and a second trigger
    rejects any statement that tries to set it directly. Making it a plain
    column with "please use the service" in a docstring is exactly the kind of
    convention that survives until the first hotfix.

 2. **Transactions are a table, not a JSON array.** The spec sketches
    `transactionHistory: [...]` inline. A JSON array cannot be indexed for the
    per-hazard-class site rollup the threshold engine runs on every receipt, it
    cannot be append-only-enforced, and a partial write silently truncates the
    ledger. The audit value of the ledger comes from it being rows.

 3. **`sdsAttachmentId` is denormalised onto ChemicalMaster.** Rule §1 requires
    the data layer to refuse ACTIVE without an SDS. A Postgres CHECK cannot
    reference another table, so the FK column lives here and the CHECK reads
    `status <> 'ACTIVE' OR sdsAttachmentId IS NOT NULL`. The Attachment row
    remains the source of truth for the file itself.

 4. **Storage locations hang off FireZone.** Per §6, this module does not build
    a second location hierarchy. `zoneId` → `FireZone.id`, which is already a
    child of Plant/Building.

 5. **ThresholdRule is config, never code.** Region + hazard class + schedule
    reference are rows. That is what makes the GCC remap a data change.

 6. **MocTriggerLog records SKIPPED as loudly as FIRED and FAILED.** A trigger
    that evaluated and decided not to act is a different fact from a trigger
    that crashed, and the whole reason this module exists in its current shape
    is that those two were previously indistinguishable.

Prisma owns the DDL (prisma/apply-chemical-ddl.ts); this is the SQLAlchemy
mirror the FastAPI layer reads and writes through, matching the convention used
by models/moc.py and models/fire_safety.py. Column names are camelCase to match.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin, SoftDeleteMixin


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


# ── vocabularies (kept as module constants, validated in the service layer) ───
# Strings, not SQL enums, for the same reason models/moc.py gives: adding a
# hazard class must not require a migration and a lock on a live table.

HAZARD_CLASSES = (
    "FLAMMABLE",
    "CORROSIVE",
    "TOXIC",
    "OXIDIZER",
    "REACTIVE",
    "CARCINOGEN",
    "EXPLOSIVE",
    "COMPRESSED_GAS",
    "PYROPHORIC",
    "WATER_REACTIVE",
    "ENVIRONMENTAL_HAZARD",
    "IRRITANT",
)
PHYSICAL_STATES = ("SOLID", "LIQUID", "GAS")
CHEMICAL_STATUSES = ("PENDING_SDS", "ACTIVE", "INACTIVE", "RESTRICTED")
TRANSACTION_TYPES = ("RECEIPT", "ISSUE", "TRANSFER_IN", "TRANSFER_OUT", "DISPOSAL", "ADJUSTMENT")
INVENTORY_STATUSES = ("IN_STOCK", "LOW", "EXPIRED", "DISPOSED")
STORAGE_TYPES = (
    "FLAMMABLE_CABINET",
    "VENTILATED_STORE",
    "COLD_STORE",
    "GENERAL",
    "OUTDOOR_BUND",
)
INCOMPATIBILITY_SEVERITIES = ("BLOCK", "WARN")
TRIGGER_OBLIGATIONS = (
    "ON_SITE_EMERGENCY_PLAN",
    "OFF_SITE_EMERGENCY_PLAN",
    "SAFETY_REPORT",
    "LICENSE_UPGRADE",
)
MOC_TRIGGER_TYPES = ("NEW_CHEMICAL", "THRESHOLD_BREACH", "STORAGE_CHANGE", "SUPPLIER_CHANGE")
MOC_TRIGGER_STATUSES = ("FIRED", "FAILED", "SKIPPED")

#: SDS validity when a ThresholdRule/tenant config does not override it (§3).
DEFAULT_SDS_VALIDITY_YEARS = 3


# ── ChemicalMaster ────────────────────────────────────────────────────────────
class ChemicalMaster(Base, IdMixin, SoftDeleteMixin):
    """Identity + hazard classification for a substance, one row per tenant.

    Hazard classification is entered by a human reading the SDS. The SDS PDF is
    attached as supporting evidence and is NOT parsed: AI/OCR extraction of
    flash point, NFPA rating and hazard phrases is a separate gated add-on and
    is deliberately out of scope here (build spec §0/§8). `flashPointCelsius`
    and `nfpaHealth/Flammability/Reactivity` are therefore ordinary
    human-entered fields, and `hazardClassificationSource` records that
    provenance so a later extraction feature cannot silently overwrite a
    human's classification without the difference being visible.
    """

    __tablename__ = "ChemicalMaster"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)

    name: Mapped[str] = mapped_column(String, nullable=False)
    commonName: Mapped[str | None] = mapped_column(String)
    casNumber: Mapped[str | None] = mapped_column(String, index=True)
    unNumber: Mapped[str | None] = mapped_column(String, index=True)

    #: Multi-select from HAZARD_CLASSES. A JSONB array rather than a join table:
    #: it is read on every threshold evaluation and never queried independently.
    hazardClasses: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    physicalState: Mapped[str] = mapped_column(String, nullable=False, default="LIQUID")

    flashPointCelsius: Mapped[float | None] = mapped_column(Float)
    boilingPointCelsius: Mapped[float | None] = mapped_column(Float)
    nfpaHealth: Mapped[int | None] = mapped_column(Integer)
    nfpaFlammability: Mapped[int | None] = mapped_column(Integer)
    nfpaReactivity: Mapped[int | None] = mapped_column(Integer)
    nfpaSpecial: Mapped[str | None] = mapped_column(String)
    #: MANUAL | IMPORTED. Never "EXTRACTED" from this module — see class docstring.
    hazardClassificationSource: Mapped[str] = mapped_column(
        String, nullable=False, default="MANUAL"
    )

    # ── SDS ──────────────────────────────────────────────────────────────────
    # Denormalised FK so the ACTIVE-requires-SDS rule is a DB CHECK, not a form
    # validation. Points at the shared evidence-attachment layer (basic tier:
    # upload/store/view). NOT parsed.
    sdsAttachmentId: Mapped[str | None] = mapped_column(String, index=True)
    sdsRevisionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sdsReviewDueDate: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    #: Set by the nightly batch when sdsReviewDueDate passes. A visible
    #: compliance signal — it deliberately does NOT change `status` (rule §6:
    #: a paperwork lapse must not deactivate a chemical people are using).
    sdsReviewOverdue: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    sdsReviewFlaggedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING_SDS", index=True
    )
    restrictionReason: Mapped[str | None] = mapped_column(Text)

    approvedByUserId: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Free text for the HIRA hazard-row linkage (§4.8) — the regulatory clause
    #: this chemical's classification derives from, propagated to hazard rows.
    regulatoryReference: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        # NB: the live constraint is a functional unique INDEX on
        # (tenantId, name, COALESCE(casNumber,'')) — see apply-chemical-ddl.ts.
        # A plain UniqueConstraint over a nullable casNumber would not prevent
        # duplicates, because Postgres treats NULLs as distinct. Declared here
        # in its plain form only so Alembic autogenerate does not propose
        # dropping it; the DDL applier owns the real definition.
        UniqueConstraint("tenantId", "name", "casNumber", name="uq_ChemicalMaster_identity"),
        Index("ix_ChemicalMaster_tenant_status", "tenantId", "status"),
        Index("ix_ChemicalMaster_sds_due", "sdsReviewDueDate", "sdsReviewOverdue"),
    )


# ── StorageLocation ───────────────────────────────────────────────────────────
class ChemicalStorageLocation(Base, IdMixin, SoftDeleteMixin):
    """A physical store, cabinet or bund that chemical stock sits in.

    Named `ChemicalStorageLocation` rather than `StorageLocation` on purpose:
    the platform already has Plant → Building → Area and FireZone, and a bare
    "StorageLocation" reads like a fifth general-purpose location entity. This
    is a *chemical* container hanging off `FireZone` (§6 — do not build a second
    location hierarchy), which is what makes a co-storage rule and a fire zone
    talk about the same physical space.
    """

    __tablename__ = "ChemicalStorageLocation"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    #: → FireZone.id. Reused, never duplicated.
    zoneId: Mapped[str | None] = mapped_column(String, index=True)

    code: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    storageType: Mapped[str] = mapped_column(String, nullable=False, default="GENERAL")

    maxCapacity: Mapped[float | None] = mapped_column(Float)
    capacityUnit: Mapped[str | None] = mapped_column(String)
    #: Computed from the ledger by the same trigger that maintains item
    #: quantities — never written by the application.
    currentOccupancy: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    ventilated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bunded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    temperatureControlled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("tenantId", "plantId", "code", name="uq_ChemStorageLoc_code"),
        Index("ix_ChemStorageLoc_plant_active", "plantId", "isActive"),
    )


# ── ChemicalInventoryItem ─────────────────────────────────────────────────────
class ChemicalInventoryItem(Base, IdMixin, SoftDeleteMixin):
    """One batch/lot of one chemical at one storage location.

    `quantityLedger` is READ-ONLY to the application. It is recomputed by a
    database trigger from `ChemicalInventoryTransaction` on every ledger write,
    and a BEFORE UPDATE trigger raises if a statement tries to change it
    directly. `currentStatus` is derived the same way. Between them, business
    rule §5 ("never a directly-editable field") holds against ad-hoc SQL, a
    future ORM bulk update, and a well-meaning hotfix — not just against the
    form the UI happens to render today.
    """

    __tablename__ = "ChemicalInventoryItem"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)
    chemicalId: Mapped[str] = mapped_column(
        ForeignKey("ChemicalMaster.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    storageLocationId: Mapped[str | None] = mapped_column(
        ForeignKey("ChemicalStorageLocation.id", ondelete="RESTRICT"), index=True
    )

    batchLotNumber: Mapped[str] = mapped_column(String, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False, default="KG")

    #: Ledger-derived. Trigger-maintained; direct UPDATE is rejected.
    quantityLedger: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Trigger-maintained too (IN_STOCK | LOW | EXPIRED | DISPOSED).
    currentStatus: Mapped[str] = mapped_column(
        String, nullable=False, default="IN_STOCK", index=True
    )
    #: Threshold below which the ledger trigger marks the item LOW.
    lowStockThreshold: Mapped[float | None] = mapped_column(Float)

    receiptDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiryDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    supplierName: Mapped[str | None] = mapped_column(String)
    supplierBatchRef: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)

    chemical: Mapped[ChemicalMaster] = relationship(lazy="joined")
    storageLocation: Mapped[ChemicalStorageLocation | None] = relationship(lazy="joined")
    transactions: Mapped[list[ChemicalInventoryTransaction]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="ChemicalInventoryTransaction.transactedAt"
    )

    __table_args__ = (
        UniqueConstraint(
            "tenantId", "chemicalId", "plantId", "batchLotNumber",
            name="uq_ChemInvItem_batch",
        ),
        Index("ix_ChemInvItem_plant_status", "plantId", "currentStatus"),
        Index("ix_ChemInvItem_location", "storageLocationId", "currentStatus"),
        Index("ix_ChemInvItem_chem_plant", "chemicalId", "plantId"),
    )


# ── ChemicalInventoryTransaction (the ledger) ─────────────────────────────────
class ChemicalInventoryTransaction(Base, IdMixin):
    """Append-only ledger row. The ONLY writable surface for inventory quantity.

    No SoftDeleteMixin and no update path by design: a correction is a
    compensating ADJUSTMENT row with a reason, not an edit. That is the same
    tamper-evidence principle the platform's other registers use, and it is what
    makes a stock-verification audit meaningful — you can only reconcile against
    a ledger nobody can quietly rewrite.

    `signedQuantity` is stored rather than derived from `type` so the trigger's
    SUM is a plain aggregate: a CASE over a string column in a hot trigger is
    both slower and one typo away from a silently wrong balance.
    """

    __tablename__ = "ChemicalInventoryTransaction"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)
    itemId: Mapped[str] = mapped_column(
        ForeignKey("ChemicalInventoryItem.id", ondelete="CASCADE"), nullable=False, index=True
    )

    type: Mapped[str] = mapped_column(String, nullable=False)
    #: Always positive — the human-facing magnitude.
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    #: +quantity for RECEIPT/TRANSFER_IN, -quantity for ISSUE/TRANSFER_OUT/
    #: DISPOSAL, and either sign for ADJUSTMENT. Set by the service layer;
    #: a CHECK constraint enforces the sign matches the type.
    signedQuantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)

    transactedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    byUserId: Mapped[str] = mapped_column(String, nullable=False)
    refDocument: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text)

    #: For TRANSFER_OUT/TRANSFER_IN — the counterpart item, so a transfer is
    #: reconcilable from either end.
    counterpartItemId: Mapped[str | None] = mapped_column(String, index=True)
    #: Set on DISPOSAL rows once the DisposalRecord exists.
    disposalRecordId: Mapped[str | None] = mapped_column(String, index=True)

    createdAt: Mapped[datetime] = _created()

    item: Mapped[ChemicalInventoryItem] = relationship(back_populates="transactions")

    __table_args__ = (
        Index("ix_ChemInvTxn_item_date", "itemId", "transactedAt"),
        Index("ix_ChemInvTxn_type_date", "type", "transactedAt"),
    )


# ── IncompatibilityMatrix ─────────────────────────────────────────────────────
class ChemicalIncompatibilityRule(Base, IdMixin, SoftDeleteMixin):
    """Co-storage rule between two hazard classes, or between two specific
    chemicals when a named pair needs to override the class-level rule.

    `severity = BLOCK` is enforced at save time by a service-layer guard AND a
    database constraint trigger (business rule §4 — "a hard constraint at save
    time, not a UI-only warning"). `WARN` permits the save but demands a logged
    override reason, mirroring the auditor-independence waiver pattern in CAMS
    rather than inventing a second override vocabulary.
    """

    __tablename__ = "ChemicalIncompatibilityRule"

    tenantId: Mapped[str | None] = mapped_column(String, index=True)  # NULL = platform default

    hazardClassA: Mapped[str | None] = mapped_column(String, index=True)
    hazardClassB: Mapped[str | None] = mapped_column(String, index=True)
    #: Specific-pair exception. When set, takes precedence over the class rule.
    chemicalIdA: Mapped[str | None] = mapped_column(String, index=True)
    chemicalIdB: Mapped[str | None] = mapped_column(String, index=True)

    severity: Mapped[str] = mapped_column(String, nullable=False, default="WARN")
    regulatoryReference: Mapped[str | None] = mapped_column(String)
    rationale: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_ChemIncompat_classes", "hazardClassA", "hazardClassB", "isActive"),
        Index("ix_ChemIncompat_chems", "chemicalIdA", "chemicalIdB", "isActive"),
    )


class ChemicalStorageOverride(Base, IdMixin):
    """A WARN-severity co-storage exception someone accepted, with the reason.

    Append-only, and surfaced on the Daily Brief as "incompatible-storage
    overrides pending review" (§6). An override with no reviewer is a decision
    nobody owns — that is why `reviewedByUserId` is nullable but tracked rather
    than absent.
    """

    __tablename__ = "ChemicalStorageOverride"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    storageLocationId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    inventoryItemId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    conflictingItemId: Mapped[str | None] = mapped_column(String)
    ruleId: Mapped[str | None] = mapped_column(String)

    severity: Mapped[str] = mapped_column(String, nullable=False, default="WARN")
    overrideReason: Mapped[str] = mapped_column(Text, nullable=False)
    overriddenByUserId: Mapped[str] = mapped_column(String, nullable=False)
    overriddenAt: Mapped[datetime] = _created()

    reviewedByUserId: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewOutcome: Mapped[str | None] = mapped_column(String)  # ACCEPTED | REVERSED

    __table_args__ = (
        Index("ix_ChemStorageOverride_pending", "plantId", "reviewedAt"),
    )


# ── ThresholdRule ─────────────────────────────────────────────────────────────
class ChemicalThresholdRule(Base, IdMixin, SoftDeleteMixin):
    """Regulatory quantity threshold for a hazard class or a specific chemical.

    Config, not code (business rule §2). `tenantId IS NULL` means a platform
    default that every tenant inherits; a tenant row with the same
    (region, scope) overrides it. `region` defaults to IN and is the seam the
    GCC remap turns — a new regulatory regime is a set of rows, not a release.
    """

    __tablename__ = "ChemicalThresholdRule"

    tenantId: Mapped[str | None] = mapped_column(String, index=True)  # NULL = platform default
    region: Mapped[str] = mapped_column(String, nullable=False, default="IN", index=True)

    hazardClass: Mapped[str | None] = mapped_column(String, index=True)
    chemicalId: Mapped[str | None] = mapped_column(String, index=True)

    scheduleReference: Mapped[str] = mapped_column(String, nullable=False)
    thresholdQuantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False, default="KG")
    #: Fraction of thresholdQuantity at which the site is "approaching" and gets
    #: a Daily Brief card — before the breach, which is the only point at which
    #: the information is still actionable.
    approachRatio: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)

    triggerObligation: Mapped[str] = mapped_column(String, nullable=False)
    autoMocOnBreach: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    notes: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_ChemThresholdRule_lookup", "region", "hazardClass", "isActive"),
        Index("ix_ChemThresholdRule_chem", "region", "chemicalId", "isActive"),
    )


class ChemicalThresholdState(Base, IdMixin):
    """Current standing of one (plant, rule) pair against its threshold.

    Exists so the threshold engine is edge-triggered rather than level-
    triggered. Without it, every receipt while a site sits above a threshold
    would raise another MOC — the failure mode that trains people to close MOCs
    without reading them. `status` transitions are what fire; a steady state
    fires nothing.
    """

    __tablename__ = "ChemicalThresholdState"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ruleId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    #: BELOW | APPROACHING | BREACHED
    status: Mapped[str] = mapped_column(String, nullable=False, default="BELOW", index=True)
    currentQuantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    thresholdQuantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str] = mapped_column(String, nullable=False, default="KG")

    lastEvaluatedAt: Mapped[datetime] = _updated()
    lastBreachedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastClearedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The MOC raised for the current breach episode, if any.
    activeMocId: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("tenantId", "plantId", "ruleId", name="uq_ChemThresholdState"),
        Index("ix_ChemThresholdState_status", "plantId", "status"),
    )


# ── MocTriggerLog ─────────────────────────────────────────────────────────────
class MocTriggerLog(Base, IdMixin):
    """Explicit audit row for every automatic MOC-trigger evaluation.

    Written by `app.services.chemical_threshold` through the shared
    `trigger_engine`, which guarantees `failureReason` is non-empty whenever
    `status = 'FAILED'` — the spec calls that out because the previous
    generation of triggers could fail with nothing recorded at all. A CHECK
    constraint enforces the same invariant at the database level so the
    guarantee does not depend on every future caller remembering it.
    """

    __tablename__ = "MocTriggerLog"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)
    plantId: Mapped[str | None] = mapped_column(String, index=True)

    triggeredAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    triggerType: Mapped[str] = mapped_column(String, nullable=False, index=True)

    sourceEntityType: Mapped[str | None] = mapped_column(String)
    sourceEntityId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    #: NULL when creation failed or was skipped.
    mocId: Mapped[str | None] = mapped_column(String, index=True)
    mocNumber: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    #: NEVER silently empty on FAILED — CHECK-enforced.
    failureReason: Mapped[str | None] = mapped_column(Text)
    stackTrace: Mapped[str | None] = mapped_column(Text)

    ruleId: Mapped[str | None] = mapped_column(String, index=True)
    scheduleReference: Mapped[str | None] = mapped_column(String)
    observedQuantity: Mapped[float | None] = mapped_column(Float)
    thresholdQuantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String)

    notifiedUserCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acknowledgedByUserId: Mapped[str | None] = mapped_column(String)
    acknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = _created()

    __table_args__ = (
        Index("ix_MocTriggerLog_status_time", "status", "triggeredAt"),
        Index("ix_MocTriggerLog_plant_time", "plantId", "triggeredAt"),
        Index("ix_MocTriggerLog_source", "sourceEntityType", "sourceEntityId"),
    )


# ── DisposalRecord ────────────────────────────────────────────────────────────
class ChemicalDisposalRecord(Base, IdMixin, SoftDeleteMixin):
    """Hazardous-waste disposal, with the manifest that makes it defensible.

    Manifest reference and vendor are NOT NULL: a disposal without them is the
    record a Pollution Control Board inspection asks for and the one that cannot
    be produced later. Feeds the EAI Register as an aspect/impact entry (§4.7).
    """

    __tablename__ = "ChemicalDisposalRecord"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    inventoryItemId: Mapped[str] = mapped_column(
        ForeignKey("ChemicalInventoryItem.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    chemicalId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    disposalDate: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    manifestReference: Mapped[str] = mapped_column(String, nullable=False)
    disposalVendor: Mapped[str] = mapped_column(String, nullable=False)
    vendorAuthorisationNo: Mapped[str | None] = mapped_column(String)
    wasteCategory: Mapped[str | None] = mapped_column(String)
    disposalMethod: Mapped[str | None] = mapped_column(String)

    #: Attachment.id of the scanned manifest (evidence layer, basic tier).
    manifestAttachmentId: Mapped[str | None] = mapped_column(String)
    #: EaiEntry.id created from this disposal, when the EAI module is enabled.
    eaiEntryId: Mapped[str | None] = mapped_column(String, index=True)

    recordedByUserId: Mapped[str] = mapped_column(String, nullable=False)

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_ChemDisposal_plant_date", "plantId", "disposalDate"),
        Index("ix_ChemDisposal_manifest", "manifestReference"),
    )


__all__ = [
    "ChemicalMaster",
    "ChemicalStorageLocation",
    "ChemicalInventoryItem",
    "ChemicalInventoryTransaction",
    "ChemicalIncompatibilityRule",
    "ChemicalStorageOverride",
    "ChemicalThresholdRule",
    "ChemicalThresholdState",
    "MocTriggerLog",
    "ChemicalDisposalRecord",
    "HAZARD_CLASSES",
    "PHYSICAL_STATES",
    "CHEMICAL_STATUSES",
    "TRANSACTION_TYPES",
    "INVENTORY_STATUSES",
    "STORAGE_TYPES",
    "INCOMPATIBILITY_SEVERITIES",
    "TRIGGER_OBLIGATIONS",
    "MOC_TRIGGER_TYPES",
    "MOC_TRIGGER_STATUSES",
    "DEFAULT_SDS_VALIDITY_YEARS",
]
