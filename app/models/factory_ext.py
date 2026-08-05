"""Facilities module — extension models (Equipment, Hazardous Materials,
Regulatory Registrations, Lifecycle workflow events).

These extend the Factory Profile Master with the asset/compliance/governance
layers from the Facilities build spec. Same conventions as ``factory.py``:
camelCase columns (schema owned by Prisma / the hand-applied DDL — see
``prisma/apply-factory-ext-ddl.ts``); ``siteId`` is a plain denormalised
``Plant.id`` reference (no FK), while the intra-module
``→ FactoryProfile`` link uses a real ForeignKey with ON DELETE CASCADE.
Every table carries the standard audit columns + ``isDeleted`` soft-delete flag.

No reverse ``relationship()`` is declared on ``FactoryProfile`` for these: the
detail endpoint loads them with explicit ``select()`` queries (matching the
existing children), and the soft-delete cascade is handled in the router's
``delete_profile`` (the DB FK cascade only fires on a HARD delete).
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
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


# ── Factory Equipment (facility-grade asset register w/ PUWER/LOLER/noise) ────
class FactoryEquipment(Base, IdMixin):
    """A production / utility asset on the factory floor, with the statutory
    inspection regimes (PUWER, LOLER, electrical, noise), maintenance state and
    operator-certification it carries. Distinct from the platform ``Equipment``
    model (which is Plant-scoped and wired into Inspections / PTW) — this one is
    facility-scoped and descriptive. ``buildingId`` is an optional loose link to
    a ``Building`` row (no FK: a building may be archived independently)."""

    __tablename__ = "FactoryEquipment"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    buildingId: Mapped[str | None] = mapped_column(String)  # loose link to Building.id

    # identity
    equipmentName: Mapped[str] = mapped_column(String, nullable=False)
    assetCode: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    manufacturer: Mapped[str | None] = mapped_column(String)
    modelNumber: Mapped[str | None] = mapped_column(String)
    serialNumber: Mapped[str | None] = mapped_column(String)
    installationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warrantyExpiryDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # operational
    capacity: Mapped[float | None] = mapped_column(Float)
    capacityUnit: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")  # ACTIVE|IDLE|DOWN|RETIRED
    operatingHoursPerDay: Mapped[float | None] = mapped_column(Float)
    hazardLevel: Mapped[str] = mapped_column(String, nullable=False, default="LOW")  # LOW|MEDIUM|HIGH

    # statutory inspection regimes (required? + last + next-due)
    puwerRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    puwerLastInspection: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    puwerNextDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lolerRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lolerLastInspection: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lolerNextDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    electricalSafetyRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    electricalLastCheck: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    electricalNextDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    noiseAssessmentRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    noiseLastTest: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    noiseMeasurementDb: Mapped[float | None] = mapped_column(Float)

    # maintenance
    lastMaintenanceDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastMaintenanceType: Mapped[str | None] = mapped_column(String)
    nextScheduledDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downtimeHoursYtd: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    # last recorded statutory inspection (see FactoryEquipmentInspection for the log)
    lastInspectionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastInspectionResult: Mapped[str | None] = mapped_column(String)  # PASS | FAIL | CONDITIONAL_PASS

    # training + spares (JSON to mirror the keyHazards/attachmentIds convention)
    certifiedOperators: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{name, certifiedOn, expiresOn}]
    spareParts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{partName, quantityInStock, reorderLevel, vendor}]

    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_FactoryEquipment_factoryProfileId", "factoryProfileId"),
        Index("ix_FactoryEquipment_siteId", "siteId"),
        Index("ix_FactoryEquipment_status", "status"),
    )


# ── Hazardous Material (chemical inventory + GHS / SDS / storage / shelf-life) ─
class HazardousMaterial(Base, IdMixin):
    """A hazardous chemical held on site: GHS classification, SDS reference,
    storage + secondary-containment, shelf-life, and trained-handler coverage.
    The ``shelfLifeStatus`` / containment / utilisation signals are computed on
    read (see ``services/factory_ext.py``)."""

    __tablename__ = "HazardousMaterial"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)

    chemicalName: Mapped[str] = mapped_column(String, nullable=False)
    casNumber: Mapped[str | None] = mapped_column(String)
    regulatoryClassification: Mapped[str | None] = mapped_column(String)  # SCHEDULED_SUBSTANCE|HIGH_HAZARD|NOTIFIED|OTHER
    hazmatClassification: Mapped[str] = mapped_column(String, nullable=False, default="LOW")  # HIGH|MEDIUM|LOW

    # GHS
    ghsSignalWord: Mapped[str | None] = mapped_column(String)  # DANGER|WARNING|NONE
    ghsHazardClasses: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ghsPictograms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # inventory
    quantityStored: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    unit: Mapped[str | None] = mapped_column(String)
    maxAllowableQty: Mapped[float | None] = mapped_column(Float)
    reorderLevel: Mapped[float | None] = mapped_column(Float)

    # storage
    storageBuilding: Mapped[str | None] = mapped_column(String)
    storageRoom: Mapped[str | None] = mapped_column(String)
    containerType: Mapped[str | None] = mapped_column(String)
    containerCount: Mapped[int | None] = mapped_column(Integer)
    secondaryContainmentPresent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    secondaryContainmentVolume: Mapped[float | None] = mapped_column(Float)
    ventilationAvailable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    signagePresent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # shelf-life
    issueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiryDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    batchLotNumber: Mapped[str | None] = mapped_column(String)

    # SDS
    sdsDocId: Mapped[str | None] = mapped_column(String)
    sdsVersion: Mapped[str | None] = mapped_column(String)
    sdsGhsCompliant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # safety
    ppeRequired: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    incompatibleSubstances: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    spillKitLocation: Mapped[str | None] = mapped_column(String)
    emergencyContact: Mapped[str | None] = mapped_column(String)

    # training coverage
    handlersTrainedCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    handlersTotalCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # regulatory
    pcbNotificationRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pcbRegistrationStatus: Mapped[str] = mapped_column(String, nullable=False, default="NOT_REGISTERED")  # REGISTERED|PENDING|NOT_REGISTERED

    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_HazardousMaterial_factoryProfileId", "factoryProfileId"),
        Index("ix_HazardousMaterial_siteId", "siteId"),
        Index("ix_HazardousMaterial_classification", "hazmatClassification"),
    )


# ── Regulatory Registration (Factory Act / ESI / Fire / PCB / Boiler …) ───────
class RegulatoryRegistration(Base, IdMixin):
    """A statutory licence / registration the factory must hold and renew. The
    ``status`` (VALID|EXPIRING_SOON|EXPIRED|PENDING_RENEWAL|SUSPENDED) is
    computed on read from ``expiryDate`` + ``alertThresholdDays`` +
    ``renewalInProgress`` (see ``services/factory_ext.py``)."""

    __tablename__ = "RegulatoryRegistration"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)

    registrationType: Mapped[str] = mapped_column(String, nullable=False)  # FACTORY_ACT|ESI|PF|GST|FIRE_LICENSE|PCB|BOILER|BUILDING_CERT|OTHER
    registrationName: Mapped[str] = mapped_column(String, nullable=False)
    registrationNumber: Mapped[str | None] = mapped_column(String)
    issuingAuthority: Mapped[str | None] = mapped_column(String)
    issueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiryDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    renewalFrequency: Mapped[str] = mapped_column(String, nullable=False, default="ANNUAL")  # ANNUAL|BIENNIAL|TRIENNIAL|ONEOFF|ONGOING
    lastRenewedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextRenewalDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, nullable=False, default="VALID")
    renewalInProgress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    renewalAgencyContact: Mapped[str | None] = mapped_column(String)
    renewalEstimatedCost: Mapped[float | None] = mapped_column(Float)
    renewalNotes: Mapped[str | None] = mapped_column(Text)

    alertThresholdDays: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    complianceImpactIfExpired: Mapped[str] = mapped_column(String, nullable=False, default="MEDIUM")  # CRITICAL|HIGH|MEDIUM|LOW
    documentationIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # P2-8: canonical link to the governed LegalObligation (single source of truth).
    # Post-migration this row is a read-only display alias of its obligation.
    legalObligationId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_RegulatoryRegistration_factoryProfileId", "factoryProfileId"),
        Index("ix_RegulatoryRegistration_siteId", "siteId"),
        Index("ix_RegulatoryRegistration_type", "registrationType"),
        Index("ix_RegulatoryRegistration_expiry", "expiryDate"),
    )


# ── Factory Lifecycle Event (append-only audit of the approval workflow) ──────
class FactoryLifecycleEvent(Base, IdMixin):
    """One transition in a factory's lifecycle workflow
    (INITIATED → EXECUTION → VALIDATION → ACTIVE, with revision loop-backs).
    Append-only: rows are never updated or deleted, so they form the immutable
    timeline shown on the workflow stepper + Audit Trail."""

    __tablename__ = "FactoryLifecycleEvent"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)

    fromStage: Mapped[str | None] = mapped_column(String)
    toStage: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)  # INITIATE|ADVANCE|REQUEST_REVISIONS|REJECT|NOTIFY
    performedBy: Mapped[str | None] = mapped_column(String)
    performedByRole: Mapped[str | None] = mapped_column(String)
    comment: Mapped[str | None] = mapped_column(Text)
    validations: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # {fieldName: bool}
    issues: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{section, issue}]

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_FactoryLifecycleEvent_factoryProfileId", "factoryProfileId"),
        Index("ix_FactoryLifecycleEvent_siteId", "siteId"),
    )


# ── Factory Equipment Inspection (statutory inspection log per asset) ──────────
class FactoryEquipmentInspection(Base, IdMixin):
    """A recorded statutory inspection against a ``FactoryEquipment`` asset
    (PUWER/LOLER-style competent-person check). Each row rolls the parent
    equipment's cached ``lastInspectionDate`` / ``lastInspectionResult`` +
    regime next-due dates forward (see the router's inspection endpoint)."""

    __tablename__ = "FactoryEquipmentInspection"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    equipmentId: Mapped[str] = mapped_column(ForeignKey("FactoryEquipment.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)

    inspectionDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    inspectorName: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(String, nullable=False)  # PASS | FAIL | CONDITIONAL_PASS
    findings: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_FactoryEquipmentInspection_equipmentId", "equipmentId"),
        Index("ix_FactoryEquipmentInspection_factoryProfileId", "factoryProfileId"),
        Index("ix_FactoryEquipmentInspection_siteId", "siteId"),
    )
