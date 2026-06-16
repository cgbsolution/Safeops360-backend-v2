"""EPC (Engineering, Procurement & Construction) Module.

SQLAlchemy mirror of the Prisma EPC models. camelCase columns match Prisma
exactly. Covers the full contractor lifecycle: site setup, contractor company
prequalification, worker registration, mobilization, site induction, and gate
clearance. The gate clearance subsystem (GateClearanceCheck / GatePass) is the
real-time enforcement layer that prevents unchecked workers from entering a
construction site.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class ConstructionSite(Base, IdMixin):
    __tablename__ = "ConstructionSite"

    tenantId: Mapped[str | None] = mapped_column(String)
    siteCode: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    siteName: Mapped[str] = mapped_column(String, nullable=False)
    projectNumber: Mapped[str | None] = mapped_column(String)

    clientName: Mapped[str | None] = mapped_column(String)
    clientContactName: Mapped[str | None] = mapped_column(String)
    clientContactEmail: Mapped[str | None] = mapped_column(String)
    clientProjectManager: Mapped[str | None] = mapped_column(String)
    clientSafetyDocUrl: Mapped[str | None] = mapped_column(String)

    address: Mapped[str | None] = mapped_column(String)
    district: Mapped[str | None] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(String)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)

    projectType: Mapped[str | None] = mapped_column(String)
    scopeDescription: Mapped[str | None] = mapped_column(String)
    contractValue: Mapped[float | None] = mapped_column(Float)
    contractCurrency: Mapped[str] = mapped_column(String, nullable=False, default="INR")

    status: Mapped[str] = mapped_column(String, nullable=False, default="awarded_setup")
    awardDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plannedStartDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plannedCompletionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualStartDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualCompletionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    peakWorkforcePlanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    siteManagerUserId: Mapped[str | None] = mapped_column(String)
    siteHseManagerUserId: Mapped[str | None] = mapped_column(String)
    siteQualityManagerUserId: Mapped[str | None] = mapped_column(String)
    corporateHseOwnerUserId: Mapped[str | None] = mapped_column(String)

    statutoryApprovals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    createdById: Mapped[str | None] = mapped_column(String)

    mobilizationRecords: Mapped[list["MobilizationRecord"]] = relationship(
        back_populates="site"
    )
    complianceConfig: Mapped["SiteComplianceConfig | None"] = relationship(
        back_populates="site", uselist=False
    )


class ContractorCompany(Base, IdMixin):
    """Maps to the existing ContractorCompany table (extended with EPC fields).
    Use `name` (not companyName) and `code` (not companyCode) — these are the
    existing column names shared with Near Miss / Incident / Manhours modules.
    """
    __tablename__ = "ContractorCompany"

    # Existing columns (shared with Near Miss + Incident modules)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    code: Mapped[str | None] = mapped_column(String, unique=True)
    contactPerson: Mapped[str | None] = mapped_column(String)
    contactEmail: Mapped[str | None] = mapped_column(String)
    contactPhone: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    # EPC-specific columns (added by EPC module migration)
    tradeName: Mapped[str | None] = mapped_column(String)
    registrationNumber: Mapped[str | None] = mapped_column(String)
    panNumber: Mapped[str | None] = mapped_column(String)
    gstNumber: Mapped[str | None] = mapped_column(String)
    tradeCategories: Mapped[list] = mapped_column(JSONB, nullable=True, default=list)
    sizeCategory: Mapped[str | None] = mapped_column(String, default="small")
    prequalificationStatus: Mapped[str] = mapped_column(String, nullable=False, default="not_applied")
    prequalificationValidUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prequalificationScore: Mapped[float | None] = mapped_column(Float)
    prequalificationReviewedById: Mapped[str | None] = mapped_column(String)
    prequalificationReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    complianceDocuments: Mapped[list] = mapped_column(JSONB, nullable=True, default=list)
    representativeName: Mapped[str | None] = mapped_column(String)
    representativePhone: Mapped[str | None] = mapped_column(String)
    representativeEmail: Mapped[str | None] = mapped_column(String)
    safetyOfficerName: Mapped[str | None] = mapped_column(String)
    safetyOfficerPhone: Mapped[str | None] = mapped_column(String)
    suspensionHistory: Mapped[list] = mapped_column(JSONB, nullable=True, default=list)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    workers: Mapped[list["ContractorWorker"]] = relationship(back_populates="contractorCompany")
    mobilizationRecords: Mapped[list["MobilizationRecord"]] = relationship(
        back_populates="contractorCompany"
    )


class ContractorWorker(Base, IdMixin):
    __tablename__ = "ContractorWorker"

    tenantId: Mapped[str | None] = mapped_column(String)
    contractorCompanyId: Mapped[str] = mapped_column(
        ForeignKey("ContractorCompany.id"), nullable=False, index=True
    )

    workerCode: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    fullName: Mapped[str] = mapped_column(String, nullable=False)
    dateOfBirth: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gender: Mapped[str | None] = mapped_column(String)
    bloodGroup: Mapped[str | None] = mapped_column(String)
    photoUrl: Mapped[str | None] = mapped_column(String)

    aadhaarLast4: Mapped[str | None] = mapped_column(String)
    aadhaarVerified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    panNumber: Mapped[str | None] = mapped_column(String)
    pfUanNumber: Mapped[str | None] = mapped_column(String)
    esicNumber: Mapped[str | None] = mapped_column(String)

    mobileNumber: Mapped[str | None] = mapped_column(String)
    emergencyContactName: Mapped[str | None] = mapped_column(String)
    emergencyContactPhone: Mapped[str | None] = mapped_column(String)
    emergencyContactRelation: Mapped[str | None] = mapped_column(String)
    homeAddress: Mapped[str | None] = mapped_column(String)
    homeDistrict: Mapped[str | None] = mapped_column(String)
    homeState: Mapped[str | None] = mapped_column(String)

    primaryTrade: Mapped[str | None] = mapped_column(String)
    secondaryTrades: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    yearsExperience: Mapped[int | None] = mapped_column(Integer)
    educationLevel: Mapped[str | None] = mapped_column(String)
    itiTrade: Mapped[str | None] = mapped_column(String)
    itiCertificateUrl: Mapped[str | None] = mapped_column(String)

    medicalFitnessRecords: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    currentMedicalValidUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    overallStatus: Mapped[str] = mapped_column(String, nullable=False, default="active")
    biometricEnrolled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    trainingCertificates: Mapped[list] = mapped_column(JSONB, nullable=True, default=list)
    competencyRecords: Mapped[list] = mapped_column(JSONB, nullable=True, default=list)
    ppeIssuances: Mapped[list] = mapped_column(JSONB, nullable=True, default=list)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    createdById: Mapped[str | None] = mapped_column(String)

    contractorCompany: Mapped["ContractorCompany"] = relationship(back_populates="workers")


class MobilizationRecord(Base, IdMixin):
    __tablename__ = "MobilizationRecord"

    tenantId: Mapped[str | None] = mapped_column(String)
    mobilizationNumber: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    contractorWorkerId: Mapped[str] = mapped_column(
        ForeignKey("ContractorWorker.id"), nullable=False, index=True
    )
    contractorCompanyId: Mapped[str] = mapped_column(
        ForeignKey("ContractorCompany.id"), nullable=False, index=True
    )
    siteId: Mapped[str] = mapped_column(
        ForeignKey("ConstructionSite.id"), nullable=False, index=True
    )

    mobilizationType: Mapped[str | None] = mapped_column(String)
    tradeAtSite: Mapped[str | None] = mapped_column(String)
    workArea: Mapped[str | None] = mapped_column(String)
    reportingSupervisorUserId: Mapped[str | None] = mapped_column(String)
    contractorCoordinatorUserId: Mapped[str | None] = mapped_column(String)

    mobilisationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plannedDemobilisationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualDemobilisationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    preMobilisationChecks: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_checks")

    approvedById: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvalConditions: Mapped[str | None] = mapped_column(String)

    demobilisationReason: Mapped[str | None] = mapped_column(String)
    demobilisationClearance: Mapped[dict | None] = mapped_column(JSONB)
    performanceNotes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    createdById: Mapped[str | None] = mapped_column(String)

    contractorWorker: Mapped["ContractorWorker"] = relationship()
    contractorCompany: Mapped["ContractorCompany"] = relationship(
        back_populates="mobilizationRecords"
    )
    site: Mapped["ConstructionSite"] = relationship(back_populates="mobilizationRecords")


class SiteInduction(Base, IdMixin):
    __tablename__ = "SiteInduction"

    tenantId: Mapped[str | None] = mapped_column(String)

    contractorWorkerId: Mapped[str] = mapped_column(
        ForeignKey("ContractorWorker.id"), nullable=False, index=True
    )
    siteId: Mapped[str] = mapped_column(
        ForeignKey("ConstructionSite.id"), nullable=False, index=True
    )
    mobilizationRecordId: Mapped[str | None] = mapped_column(
        ForeignKey("MobilizationRecord.id"), index=True
    )

    inductionType: Mapped[str | None] = mapped_column(String)
    topicsCovered: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    clientRequirementsCovered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    siteEmergencyProceduresCovered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    siteLayoutFamiliarization: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    musterPointIdentified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ppeCoveredBool: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ptwSystemExplained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    incidentReportingExplained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    conductedById: Mapped[str | None] = mapped_column(String)
    conductedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    durationMinutes: Mapped[int | None] = mapped_column(Integer)
    inductionLanguage: Mapped[str | None] = mapped_column(String)
    interpreterUsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interpreterName: Mapped[str | None] = mapped_column(String)

    assessmentConducted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assessmentScore: Mapped[float | None] = mapped_column(Float)
    assessmentPassScore: Mapped[float] = mapped_column(Float, nullable=False, default=70.0)
    assessmentPassed: Mapped[bool | None] = mapped_column(Boolean)
    failedTopics: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    reInductionRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reInductionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    inductionPhotoUrl: Mapped[str | None] = mapped_column(String)
    groupPhotoUrl: Mapped[str | None] = mapped_column(String)

    workerAcknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    workerAcknowledgementMethod: Mapped[str | None] = mapped_column(String)
    workerAcknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledgementUrl: Mapped[str | None] = mapped_column(String)

    validFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isExpired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    templateId: Mapped[str | None] = mapped_column(String)
    templateVersion: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class GateClearanceCheck(Base, IdMixin):
    __tablename__ = "GateClearanceCheck"

    tenantId: Mapped[str | None] = mapped_column(String)

    siteId: Mapped[str] = mapped_column(
        ForeignKey("ConstructionSite.id"), nullable=False, index=True
    )
    contractorWorkerId: Mapped[str] = mapped_column(
        ForeignKey("ContractorWorker.id"), nullable=False, index=True
    )

    workerName: Mapped[str | None] = mapped_column(String)
    workerCode: Mapped[str | None] = mapped_column(String)
    contractorCompanyName: Mapped[str | None] = mapped_column(String)

    checkRequestedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkMethod: Mapped[str | None] = mapped_column(String)

    checks: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    overallResult: Mapped[str | None] = mapped_column(String)
    blockingIssues: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    warningIssues: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    gatePassIssued: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gatePassId: Mapped[str | None] = mapped_column(String)

    overrideApplied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    overrideByUserId: Mapped[str | None] = mapped_column(String)
    overrideAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    overrideReason: Mapped[str | None] = mapped_column(String)
    overrideAuthorityRole: Mapped[str | None] = mapped_column(String)

    checkCompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processingDurationMs: Mapped[int | None] = mapped_column(Integer)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GatePass(Base, IdMixin):
    __tablename__ = "GatePass"

    tenantId: Mapped[str | None] = mapped_column(String)

    siteId: Mapped[str] = mapped_column(
        ForeignKey("ConstructionSite.id"), nullable=False, index=True
    )
    clearanceCheckId: Mapped[str | None] = mapped_column(
        ForeignKey("GateClearanceCheck.id"), unique=True
    )
    contractorWorkerId: Mapped[str] = mapped_column(
        ForeignKey("ContractorWorker.id"), nullable=False, index=True
    )

    workerName: Mapped[str | None] = mapped_column(String)
    workerCode: Mapped[str | None] = mapped_column(String)
    workerPhotoUrl: Mapped[str | None] = mapped_column(String)
    primaryTrade: Mapped[str | None] = mapped_column(String)
    contractorCompanyName: Mapped[str | None] = mapped_column(String)

    passNumber: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    passType: Mapped[str | None] = mapped_column(String)
    validFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    authorizedAreas: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    authorizedTrades: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    revokedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revokedById: Mapped[str | None] = mapped_column(String)
    revocationReason: Mapped[str | None] = mapped_column(String)

    qrCodeData: Mapped[str | None] = mapped_column(String)
    generatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SiteComplianceConfig(Base, IdMixin):
    __tablename__ = "SiteComplianceConfig"

    tenantId: Mapped[str | None] = mapped_column(String)
    siteId: Mapped[str] = mapped_column(
        ForeignKey("ConstructionSite.id"), nullable=False, unique=True
    )
    clientName: Mapped[str | None] = mapped_column(String)

    corporateTemplateId: Mapped[str | None] = mapped_column(String)
    overridesApplied: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    ptwConfig: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    capaConfig: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    incidentConfig: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    inductionConfig: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    gateConfig: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    mandatoryTraining: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    minimumPpeRequirements: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    kpiTargets: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reportingConfig: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    effectiveFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersededAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedById: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    site: Mapped["ConstructionSite"] = relationship(back_populates="complianceConfig")


__all__ = [
    "ConstructionSite",
    "ContractorCompany",
    "ContractorWorker",
    "MobilizationRecord",
    "SiteInduction",
    "GateClearanceCheck",
    "GatePass",
    "SiteComplianceConfig",
]
