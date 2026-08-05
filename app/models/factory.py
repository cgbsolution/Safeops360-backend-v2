"""Facilities — Factory Profile Master SQLAlchemy models.

Mirrors the `FactoryProfile` / `Building` Prisma models in schema.prisma
(section "FACILITIES — Factory Profile Master & Consolidated Dashboard"). Schema
is owned by Prisma (db push); camelCase columns match the DB. `siteId` is a
plain String reference to Plant.id — no FK, matching the Cams* convention; the
1:1 site mapping is enforced by the unique constraint + a 409 check in the
router. Only the intra-module Building → FactoryProfile link uses ForeignKey.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


# ── Factory Profile (1:1 with Plant via siteId) ─────────────────────────────
class FactoryProfile(Base, IdMixin):
    __tablename__ = "FactoryProfile"

    siteId: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # Plant.id — 1:1
    factoryCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    factoryName: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPERATIONAL")
    ownershipType: Mapped[str] = mapped_column(String, nullable=False, default="OWNED")

    # location
    addressLine: Mapped[str] = mapped_column(String, nullable=False, default="")
    city: Mapped[str] = mapped_column(String, nullable=False, default="")
    state: Mapped[str] = mapped_column(String, nullable=False, default="")
    pincode: Mapped[str] = mapped_column(String, nullable=False, default="")
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    # identity & statutory
    establishedYear: Mapped[int | None] = mapped_column(Integer)
    factoryLicenseNo: Mapped[str | None] = mapped_column(String)
    factoryLicenseValidUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registrationNos: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    applicableActs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    pollutionControlBoard: Mapped[str | None] = mapped_column(String)

    # descriptive
    totalLandAreaSqm: Mapped[float | None] = mapped_column(Float)
    builtUpAreaSqm: Mapped[float | None] = mapped_column(Float)
    buildingCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # workforce summary (denormalised — Phase B)
    totalEmployees: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # operational summary
    primaryIndustry: Mapped[str] = mapped_column(String, nullable=False, default="Garments / Textile")
    profileStatus: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    lastReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # lifecycle workflow (INITIATED → EXECUTION → VALIDATION → ACTIVE → ARCHIVED).
    # Separate from `profileStatus` (data-completeness) — this is the governance
    # approval state. See models/factory_ext.FactoryLifecycleEvent for the history.
    lifecycleStage: Mapped[str] = mapped_column(String, nullable=False, default="INITIATED")
    lifecycleStageOwnerRole: Mapped[str | None] = mapped_column(String)
    lifecycleUpdatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    buildings: Mapped[list["Building"]] = relationship(back_populates="factoryProfile", cascade="all, delete-orphan")
    workforceCompositions: Mapped[list["WorkforceComposition"]] = relationship(back_populates="factoryProfile", cascade="all, delete-orphan")
    productionProcesses: Mapped[list["ProductionProcess"]] = relationship(back_populates="factoryProfile", cascade="all, delete-orphan")
    certifications: Mapped[list["FactoryCertification"]] = relationship(back_populates="factoryProfile", cascade="all, delete-orphan")
    contacts: Mapped[list["FactoryContact"]] = relationship(back_populates="factoryProfile", cascade="all, delete-orphan")
    complianceSnapshots: Mapped[list["FactoryComplianceSnapshot"]] = relationship(back_populates="factoryProfile", cascade="all, delete-orphan")
    envPeriods: Mapped[list["FactoryEnvPeriod"]] = relationship(back_populates="factoryProfile", cascade="all, delete-orphan")
    socialCompliance: Mapped["SocialComplianceProfile | None"] = relationship(back_populates="factoryProfile", cascade="all, delete-orphan", uselist=False)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_FactoryProfile_state", "state"),
        Index("ix_FactoryProfile_status", "status"),
        Index("ix_FactoryProfile_profileStatus", "profileStatus"),
    )


# ── Building Register ───────────────────────────────────────────────────────
class Building(Base, IdMixin):
    __tablename__ = "Building"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    buildingName: Mapped[str] = mapped_column(String, nullable=False)
    buildingType: Mapped[str] = mapped_column(String, nullable=False, default="PRODUCTION")
    floors: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    areaSqm: Mapped[float | None] = mapped_column(Float)
    maxOccupancy: Mapped[int | None] = mapped_column(Integer)
    currentOccupancy: Mapped[int | None] = mapped_column(Integer)
    yearBuilt: Mapped[int | None] = mapped_column(Integer)
    assemblyPoint: Mapped[str | None] = mapped_column(String)
    emergencyExits: Mapped[int | None] = mapped_column(Integer)
    occupancyCertificateNo: Mapped[str | None] = mapped_column(String)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    factoryProfile: Mapped["FactoryProfile"] = relationship(back_populates="buildings")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_Building_factoryProfileId", "factoryProfileId"),
        Index("ix_Building_siteId", "siteId"),
        Index("ix_Building_buildingType", "buildingType"),
    )


# ── Workforce Composition (SA8000-aware; history via isCurrent) ──────────────
class WorkforceComposition(Base, IdMixin):
    __tablename__ = "WorkforceComposition"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    asOfDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    isCurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    permanentCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contractCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    apprenticeTraineeCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    maleCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    femaleCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    otherGenderCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    migrantWorkerCount: Mapped[int | None] = mapped_column(Integer)
    differentlyAbledCount: Mapped[int | None] = mapped_column(Integer)
    totalCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # child-labour evidence (SA8000 Element 1)
    youngestWorkerAge: Mapped[int | None] = mapped_column(Integer)
    workersUnder18Count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minHiringAgePolicy: Mapped[int | None] = mapped_column(Integer)
    # derived (persisted for fast export)
    contractPct: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    femalePct: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    migrantPct: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    factoryProfile: Mapped["FactoryProfile"] = relationship(back_populates="workforceCompositions")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_WorkforceComposition_profile_current", "factoryProfileId", "isCurrent"),
        Index("ix_WorkforceComposition_siteId", "siteId"),
    )


# ── Production Process ───────────────────────────────────────────────────────
class ProductionProcess(Base, IdMixin):
    __tablename__ = "ProductionProcess"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    processName: Mapped[str] = mapped_column(String, nullable=False)
    processCategory: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    sequenceOrder: Mapped[int | None] = mapped_column(Integer)
    shiftPattern: Mapped[str | None] = mapped_column(String)
    installedCapacity: Mapped[str | None] = mapped_column(String)
    keyHazards: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    factoryProfile: Mapped["FactoryProfile"] = relationship(back_populates="productionProcesses")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_ProductionProcess_factoryProfileId", "factoryProfileId"),
        Index("ix_ProductionProcess_siteId", "siteId"),
    )


# ── Factory Certification (buyer / social-compliance certs) ──────────────────
class FactoryCertification(Base, IdMixin):
    __tablename__ = "FactoryCertification"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    certificationType: Mapped[str] = mapped_column(String, nullable=False)
    certificateNo: Mapped[str | None] = mapped_column(String)
    issuingBody: Mapped[str | None] = mapped_column(String)
    issueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiryDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    renewalLeadDays: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    status: Mapped[str] = mapped_column(String, nullable=False, default="VALID")
    scopeNotes: Mapped[str | None] = mapped_column(Text)
    attachmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    factoryProfile: Mapped["FactoryProfile"] = relationship(back_populates="certifications")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_FactoryCertification_factoryProfileId", "factoryProfileId"),
        Index("ix_FactoryCertification_siteId", "siteId"),
        Index("ix_FactoryCertification_type", "certificationType"),
    )


# ── Factory Contact ──────────────────────────────────────────────────────────
class FactoryContact(Base, IdMixin):
    __tablename__ = "FactoryContact"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="OTHER")
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    isPrimary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    factoryProfile: Mapped["FactoryProfile"] = relationship(back_populates="contacts")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_FactoryContact_factoryProfileId", "factoryProfileId"),
        Index("ix_FactoryContact_siteId", "siteId"),
    )


# ── Factory Compliance Snapshot (dashboard precompute) ───────────────────────
class FactoryComplianceSnapshot(Base, IdMixin):
    __tablename__ = "FactoryComplianceSnapshot"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    periodLabel: Mapped[str] = mapped_column(String, nullable=False, default="LIVE")
    auditComplianceScorePct: Mapped[float | None] = mapped_column(Float)
    openFindings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    criticalFindings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    openCapas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overdueCapas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    openObligations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overdueObligations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    certsExpiringCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastAuditDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incidentCount12m: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computedAt: Mapped[datetime] = _created()

    factoryProfile: Mapped["FactoryProfile"] = relationship(back_populates="complianceSnapshots")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_FactoryComplianceSnapshot_profile_period", "factoryProfileId", "periodLabel", unique=True),
        Index("ix_FactoryComplianceSnapshot_siteId", "siteId"),
    )


# ── Factory Environmental Period (ESG operational source — one row/site/period) ─
class FactoryEnvPeriod(Base, IdMixin):
    """Minimal environmental-data source for the facility ESG rollup. This is a
    genuine source-of-truth table (the "ESG module" the facility tab reads from),
    NOT a rollup cache: one row per (site, period) holds the energy / water /
    effluent / waste / emissions totals + targets the Environmental Operational
    rollup surfaces. A site with no rows ⇒ the tab renders the neutral
    "Environmental data — module not enabled" tile (graceful absence). A fuller
    ESG capture UI is roadmap; for now rows are seeded / API-managed."""

    __tablename__ = "FactoryEnvPeriod"

    factoryProfileId: Mapped[str] = mapped_column(ForeignKey("FactoryProfile.id", ondelete="CASCADE"), nullable=False)
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    periodLabel: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "2026-Q2"

    # Energy
    energyKwh: Mapped[float | None] = mapped_column(Float)
    energyIntensity: Mapped[float | None] = mapped_column(Float)  # kWh / unit output
    energyTargetKwh: Mapped[float | None] = mapped_column(Float)
    # Water + effluent (garment-critical)
    waterWithdrawnKl: Mapped[float | None] = mapped_column(Float)
    effluentDischargedKl: Mapped[float | None] = mapped_column(Float)
    etpStatus: Mapped[str | None] = mapped_column(String)  # COMPLIANT | NON_COMPLIANT | NOT_MONITORED
    consentStatus: Mapped[str | None] = mapped_column(String)  # free-text mirror of SPCB consent standing
    # Waste
    wasteGeneratedT: Mapped[float | None] = mapped_column(Float)
    wasteDivertedPct: Mapped[float | None] = mapped_column(Float)
    wasteDivertedTargetPct: Mapped[float | None] = mapped_column(Float)
    # Emissions
    scope1TCo2e: Mapped[float | None] = mapped_column(Float)
    scope2TCo2e: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    factoryProfile: Mapped["FactoryProfile"] = relationship(back_populates="envPeriods")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_FactoryEnvPeriod_profile_period", "factoryProfileId", "periodLabel", unique=True),
        Index("ix_FactoryEnvPeriod_siteId", "siteId"),
    )


# ── Social-Compliance Profile (SA8000 policy/standing; 1:1 with factory) ─────
class SocialComplianceProfile(Base, IdMixin):
    """The less-volatile attributes a buyer social-compliance audit (SA8000 /
    WRAP / BSCI / SMETA) checks — wages, working hours, freedom of association,
    forced labour, grievance, training — separate from the dated headcount.
    ComplianceFlag columns hold COMPLIANT | ATTENTION | NON_COMPLIANT |
    NOT_ASSESSED; `overallSocialComplianceFlag` is the persisted worst-of
    (computed on write)."""

    __tablename__ = "SocialComplianceProfile"

    factoryProfileId: Mapped[str] = mapped_column(
        ForeignKey("FactoryProfile.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    siteId: Mapped[str] = mapped_column(String, nullable=False)
    asOfDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Remuneration (Element 8)
    minimumWageCompliant: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")
    lowestMonthlyWageInr: Mapped[int | None] = mapped_column(Integer)
    statutoryMinimumWageInr: Mapped[int | None] = mapped_column(Integer)
    wagesPaidOnTime: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")

    # Working Hours (Element 7)
    standardWeeklyHours: Mapped[int | None] = mapped_column(Integer)
    maxWeeklyOvertimeHours: Mapped[int | None] = mapped_column(Integer)
    overtimeVoluntary: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")
    weeklyRestDayProvided: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")

    # Freedom of Association (Element 4)
    unionOrWorkerCommitteePresent: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")
    collectiveBargainingAgreement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Forced Labour (Element 2)
    noDepositOrDocumentRetention: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")

    # Disciplinary & Grievance (Elements 5, 6, 9)
    grievanceMechanismPresent: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")
    antiDiscriminationPolicy: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")

    # Management System / Training (Element 9)
    sa8000AwarenessTrainingPct: Mapped[float | None] = mapped_column(Float)
    socialComplianceOwnerId: Mapped[str | None] = mapped_column(String)
    lastSocialAuditDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    overallSocialComplianceFlag: Mapped[str] = mapped_column(String, nullable=False, default="NOT_ASSESSED")

    factoryProfile: Mapped["FactoryProfile"] = relationship(back_populates="socialCompliance")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_SocialComplianceProfile_factoryProfileId", "factoryProfileId", unique=True),
        Index("ix_SocialComplianceProfile_siteId", "siteId"),
    )
