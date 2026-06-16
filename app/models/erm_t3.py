"""ERM Tier 3 — Internal Controls · Vendor/Third-Party Risk · Insurance & Transfer.

Mirrors the Tier 3 Prisma family. camelCase columns. Plain-String refs to
existing tables (EnterpriseRisk, BusinessProcess, LegalObligation, LossEvent, Capa).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


# ── Internal Controls ──────────────────────────────────────────────────────────
class Control(Base, IdMixin):
    __tablename__ = "Control"
    controlCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    controlType: Mapped[str] = mapped_column(String, nullable=False)
    nature: Mapped[str] = mapped_column(String, nullable=False)
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    controlOwnerId: Mapped[str] = mapped_column(String, nullable=False)
    processName: Mapped[str | None] = mapped_column(String)
    siteId: Mapped[str | None] = mapped_column(String)
    isKeyControl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assertions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    controlDesignNotes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    currentDesignRating: Mapped[str | None] = mapped_column(String)
    currentOperatingRating: Mapped[str | None] = mapped_column(String)
    lastTestDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextTestDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mappings: Mapped[list["RiskControlMapping"]] = relationship(back_populates="control", cascade="all, delete-orphan")
    testPlans: Mapped[list["ControlTestPlan"]] = relationship(back_populates="control", cascade="all, delete-orphan")
    tests: Mapped[list["ControlTest"]] = relationship(back_populates="control", cascade="all, delete-orphan")
    deficiencies: Mapped[list["ControlDeficiency"]] = relationship(back_populates="control", cascade="all, delete-orphan")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_Control_cat_key", "category", "isKeyControl"), Index("ix_Control_site", "siteId"), Index("ix_Control_owner", "controlOwnerId"))


class RiskControlMapping(Base, IdMixin):
    __tablename__ = "RiskControlMapping"
    controlId: Mapped[str] = mapped_column(ForeignKey("Control.id", ondelete="CASCADE"), nullable=False)
    control: Mapped[Control] = relationship(back_populates="mappings")
    riskId: Mapped[str | None] = mapped_column(String)
    processId: Mapped[str | None] = mapped_column(String)
    obligationId: Mapped[str | None] = mapped_column(String)
    mitigationStrength: Mapped[str] = mapped_column(String, nullable=False)
    coverageNotes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_RCM_control", "controlId"), Index("ix_RCM_risk", "riskId"), Index("ix_RCM_process", "processId"), Index("ix_RCM_obl", "obligationId"))


class ControlTestPlan(Base, IdMixin):
    __tablename__ = "ControlTestPlan"
    controlId: Mapped[str] = mapped_column(ForeignKey("Control.id", ondelete="CASCADE"), nullable=False)
    control: Mapped[Control] = relationship(back_populates="testPlans")
    testCycleLabel: Mapped[str] = mapped_column(String, nullable=False)
    testMethod: Mapped[str] = mapped_column(String, nullable=False)
    sampleSizePlanned: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    testFrequencyPerYear: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    assignedTesterId: Mapped[str] = mapped_column(String, nullable=False)
    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_CTP_control", "controlId"),)


class ControlTest(Base, IdMixin):
    __tablename__ = "ControlTest"
    controlId: Mapped[str] = mapped_column(ForeignKey("Control.id", ondelete="CASCADE"), nullable=False)
    control: Mapped[Control] = relationship(back_populates="tests")
    testPlanId: Mapped[str | None] = mapped_column(String)
    testType: Mapped[str] = mapped_column(String, nullable=False)
    testDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    testerId: Mapped[str] = mapped_column(String, nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)
    sampleSize: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    exceptionsFound: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conclusion: Mapped[str] = mapped_column(String, nullable=False)
    workpaperNotes: Mapped[str] = mapped_column(Text, nullable=False)
    evidenceAttachmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    deficiencyId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_CT_control_type", "controlId", "testType"), Index("ix_CT_date", "testDate"))


class ControlDeficiency(Base, IdMixin):
    __tablename__ = "ControlDeficiency"
    deficiencyCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    controlId: Mapped[str] = mapped_column(ForeignKey("Control.id", ondelete="CASCADE"), nullable=False)
    control: Mapped[Control] = relationship(back_populates="deficiencies")
    sourceTestId: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rootCause: Mapped[str | None] = mapped_column(Text)
    remediationCapaId: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    identifiedRiskImpact: Mapped[str | None] = mapped_column(Text)
    reportedToAuditCommittee: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auditCommitteeReference: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_Def_control", "controlId"), Index("ix_Def_sev_status", "severity", "status"))


# ── Vendor / Third-Party Risk ──────────────────────────────────────────────────
class VendorProfile(Base, IdMixin):
    __tablename__ = "VendorProfile"
    vendorCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    masterDataRef: Mapped[str | None] = mapped_column(String)
    legalName: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    criticality: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)
    siteScope: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    relationshipOwnerId: Mapped[str] = mapped_column(String, nullable=False)
    annualSpendInr: Mapped[float | None] = mapped_column(Float)
    isSingleSource: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linkedProcessIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    linkedRiskIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    onboardingStatus: Mapped[str] = mapped_column(String, nullable=False, default="PROSPECT")
    currentRiskScore: Mapped[float | None] = mapped_column(Float)
    currentRiskBand: Mapped[str | None] = mapped_column(String)
    currentEsgScore: Mapped[float | None] = mapped_column(Float)
    currentEsgBand: Mapped[str | None] = mapped_column(String)
    nextReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assessments: Mapped[list["VendorAssessment"]] = relationship(back_populates="vendor", cascade="all, delete-orphan")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_Vendor_crit_risk", "criticality", "currentRiskBand"), Index("ix_Vendor_esg", "currentEsgBand"), Index("ix_Vendor_onboard", "onboardingStatus"))


class VendorAssessment(Base, IdMixin):
    __tablename__ = "VendorAssessment"
    vendorId: Mapped[str] = mapped_column(ForeignKey("VendorProfile.id", ondelete="CASCADE"), nullable=False)
    vendor: Mapped[VendorProfile] = relationship(back_populates="assessments")
    lens: Mapped[str] = mapped_column(String, nullable=False)
    assessmentDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assessorId: Mapped[str] = mapped_column(String, nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)
    domainScores: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    weightedScore: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    band: Mapped[str] = mapped_column(String, nullable=False)
    summaryNotes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    validUntil: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    isCurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_VA_vendor_lens", "vendorId", "lens"),)


class VendorScoringConfig(Base, IdMixin):
    __tablename__ = "VendorScoringConfig"
    lens: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    domains: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    bandThresholds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()


# ── Insurance & Risk Transfer ──────────────────────────────────────────────────
class InsurancePolicy(Base, IdMixin):
    __tablename__ = "InsurancePolicy"
    policyCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    policyName: Mapped[str] = mapped_column(String, nullable=False)
    policyType: Mapped[str] = mapped_column(String, nullable=False)
    insurerName: Mapped[str] = mapped_column(String, nullable=False)
    brokerName: Mapped[str | None] = mapped_column(String)
    policyNumber: Mapped[str] = mapped_column(String, nullable=False)
    siteScope: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sumInsuredInr: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    premiumAnnualInr: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    deductibleInr: Mapped[float | None] = mapped_column(Float)
    coverageStartDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    coverageEndDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewalLeadDays: Mapped[int] = mapped_column(Integer, nullable=False, default=45)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    keyExclusions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    coveredRiskIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    coveredProcessIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ownerId: Mapped[str] = mapped_column(String, nullable=False)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    claims: Mapped[list["InsuranceClaim"]] = relationship(back_populates="policy", cascade="all, delete-orphan")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_Policy_type_status", "policyType", "status"), Index("ix_Policy_status", "status"))


class InsuranceClaim(Base, IdMixin):
    __tablename__ = "InsuranceClaim"
    claimCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    policyId: Mapped[str] = mapped_column(ForeignKey("InsurancePolicy.id", ondelete="CASCADE"), nullable=False)
    policy: Mapped[InsurancePolicy] = relationship(back_populates="claims")
    lossEventId: Mapped[str | None] = mapped_column(String)
    claimDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    claimedAmountInr: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="INTIMATED")
    settledAmountInr: Mapped[float | None] = mapped_column(Float)
    settlementDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remarks: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_Claim_policy", "policyId"), Index("ix_Claim_loss", "lossEventId"), Index("ix_Claim_status", "status"))


class CoverageGapAssessment(Base, IdMixin):
    __tablename__ = "CoverageGapAssessment"
    assessmentCycleLabel: Mapped[str] = mapped_column(String, nullable=False)
    reviewDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewedBy: Mapped[str] = mapped_column(String, nullable=False)
    lines: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summaryNotes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_CGA_date", "reviewDate"),)
