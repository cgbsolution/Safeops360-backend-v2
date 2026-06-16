"""Enterprise Risk Management (ERM) — Phase 1.

Mirrors the Prisma ERM family in
[safeops_360/prisma/schema.prisma](../../../safeops_360/prisma/schema.prisma)
section "ENTERPRISE RISK MANAGEMENT (ERM)".

Schema is owned by Prisma (db push). This file lets the SQLAlchemy-side
routers read/write the same tables. camelCase column names are required to
match the DB columns Prisma created.

Tables:
  - RiskCategory, RiskSubCategory      — taxonomy
  - ScoringMatrixConfig                — 5×5 matrix (likelihood/impact/bands as JSON)
  - EnterpriseRisk                     — the register record
  - RiskAssessment                     — inherent / residual scoring (history preserved)
  - RiskLinkage                        — risk interconnection (self-referential)
  - RollupRule, RollupLinkage          — HSE rollup engine (reads HIRA/EAI)
  - ReviewCycleConfig, RiskReview      — review cadence + review events
  - ErmBoardPack                       — board pack generator state
  - ErmRiskSnapshot                    — quarter-end snapshot (trend / quarter-compare)

References to existing tables (User, Plant, Capa, HiraEntry, EaiEntry) are
plain String columns — no SQLAlchemy ForeignKey to those, matching the Prisma
decision to add zero fields to existing models.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


# ─────────────────────────────────────────────────────────────────────
# Taxonomy
# ─────────────────────────────────────────────────────────────────────
class RiskCategory(Base, IdMixin):
    __tablename__ = "RiskCategory"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    colorHex: Mapped[str] = mapped_column(String, nullable=False)
    displayOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isSystemCategory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    subCategories: Mapped[list["RiskSubCategory"]] = relationship(back_populates="category")

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RiskSubCategory(Base, IdMixin):
    __tablename__ = "RiskSubCategory"

    categoryId: Mapped[str] = mapped_column(ForeignKey("RiskCategory.id", ondelete="CASCADE"), nullable=False)
    category: Mapped[RiskCategory] = relationship(back_populates="subCategories")
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_RiskSubCategory_category_active", "categoryId", "isActive"),)


# ─────────────────────────────────────────────────────────────────────
# Scoring matrix
# ─────────────────────────────────────────────────────────────────────
class ScoringMatrixConfig(Base, IdMixin):
    __tablename__ = "ScoringMatrixConfig"

    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    isDefault: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    likelihoodLevels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    impactLevels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ratingBands: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ─────────────────────────────────────────────────────────────────────
# Enterprise risk
# ─────────────────────────────────────────────────────────────────────
class EnterpriseRisk(Base, IdMixin):
    __tablename__ = "EnterpriseRisk"

    riskCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    categoryId: Mapped[str] = mapped_column(ForeignKey("RiskCategory.id"), nullable=False)
    subCategoryId: Mapped[str | None] = mapped_column(ForeignKey("RiskSubCategory.id"))

    orgLevel: Mapped[str] = mapped_column(String, nullable=False, default="ENTERPRISE")
    businessUnit: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str | None] = mapped_column(String)
    riskOwnerId: Mapped[str] = mapped_column(String, nullable=False)
    riskChampionId: Mapped[str] = mapped_column(String, nullable=False)

    lifecycleState: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    velocity: Mapped[str] = mapped_column(String, nullable=False, default="MODERATE")
    sourceType: Mapped[str] = mapped_column(String, nullable=False, default="MANUAL")
    rollupRuleId: Mapped[str | None] = mapped_column(String)

    identifiedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    nextReviewDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    appetiteThreshold: Mapped[int | None] = mapped_column(Integer)

    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    causes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    consequences: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    existingControls: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    inherentLikelihood: Mapped[int | None] = mapped_column(Integer)
    inherentImpact: Mapped[int | None] = mapped_column(Integer)
    inherentScore: Mapped[int | None] = mapped_column(Integer)
    inherentBand: Mapped[str | None] = mapped_column(String)
    residualLikelihood: Mapped[int | None] = mapped_column(Integer)
    residualImpact: Mapped[int | None] = mapped_column(Integer)
    residualScore: Mapped[int | None] = mapped_column(Integer)
    residualBand: Mapped[str | None] = mapped_column(String)
    priorResidualScore: Mapped[int | None] = mapped_column(Integer)
    priorResidualBand: Mapped[str | None] = mapped_column(String)

    closureJustification: Mapped[str | None] = mapped_column(Text)
    acceptanceJustification: Mapped[str | None] = mapped_column(Text)
    acceptedBy: Mapped[str | None] = mapped_column(String)
    acceptedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    category: Mapped[RiskCategory] = relationship()
    subCategory: Mapped["RiskSubCategory | None"] = relationship()
    assessments: Mapped[list["RiskAssessment"]] = relationship(
        back_populates="risk", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["RiskReview"]] = relationship(
        back_populates="risk", cascade="all, delete-orphan"
    )
    rollupLinkages: Mapped[list["RollupLinkage"]] = relationship(
        back_populates="enterpriseRisk", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_EnterpriseRisk_category_state", "categoryId", "lifecycleState"),
        Index("ix_EnterpriseRisk_plant_state", "plantId", "lifecycleState"),
        Index("ix_EnterpriseRisk_owner", "riskOwnerId"),
        Index("ix_EnterpriseRisk_residual_band", "residualBand"),
        Index("ix_EnterpriseRisk_source", "sourceType"),
        Index("ix_EnterpriseRisk_review", "nextReviewDate"),
        Index("ix_EnterpriseRisk_deleted_state", "isDeleted", "lifecycleState"),
    )


class RiskAssessment(Base, IdMixin):
    __tablename__ = "RiskAssessment"

    riskId: Mapped[str] = mapped_column(ForeignKey("EnterpriseRisk.id", ondelete="CASCADE"), nullable=False)
    risk: Mapped[EnterpriseRisk] = relationship(back_populates="assessments")
    matrixConfigId: Mapped[str | None] = mapped_column(ForeignKey("ScoringMatrixConfig.id"))
    matrixVersion: Mapped[int | None] = mapped_column(Integer)

    assessmentType: Mapped[str] = mapped_column(String, nullable=False)
    likelihood: Mapped[int] = mapped_column(Integer, nullable=False)
    impactScores: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    dominantImpactDimension: Mapped[str] = mapped_column(String, nullable=False)
    overallImpact: Mapped[int] = mapped_column(Integer, nullable=False)
    totalScore: Mapped[int] = mapped_column(Integer, nullable=False)
    ratingBand: Mapped[str] = mapped_column(String, nullable=False)

    assessmentDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assessedBy: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    isCurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_RiskAssessment_risk_type_current", "riskId", "assessmentType", "isCurrent"),)


class RiskLinkage(Base, IdMixin):
    __tablename__ = "RiskLinkage"

    sourceRiskId: Mapped[str] = mapped_column(ForeignKey("EnterpriseRisk.id", ondelete="CASCADE"), nullable=False)
    targetRiskId: Mapped[str] = mapped_column(ForeignKey("EnterpriseRisk.id", ondelete="CASCADE"), nullable=False)
    linkageType: Mapped[str] = mapped_column(String, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    sourceRisk: Mapped[EnterpriseRisk] = relationship(foreign_keys=[sourceRiskId])
    targetRisk: Mapped[EnterpriseRisk] = relationship(foreign_keys=[targetRiskId])

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("sourceRiskId", "targetRiskId", name="uq_RiskLinkage_pair"),
        Index("ix_RiskLinkage_source", "sourceRiskId"),
        Index("ix_RiskLinkage_target", "targetRiskId"),
    )


# ─────────────────────────────────────────────────────────────────────
# Rollup engine
# ─────────────────────────────────────────────────────────────────────
class RollupRule(Base, IdMixin):
    __tablename__ = "RollupRule"

    name: Mapped[str] = mapped_column(String, nullable=False)
    sourceRegister: Mapped[str] = mapped_column(String, nullable=False, default="COMBINED_RISK_REGISTER")
    filterCriteria: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    aggregationMode: Mapped[str] = mapped_column(String, nullable=False, default="GROUPED")
    targetCategoryCode: Mapped[str] = mapped_column(String, nullable=False, default="OPS")
    targetSubCategoryCode: Mapped[str] = mapped_column(String, nullable=False)
    scoringMode: Mapped[str] = mapped_column(String, nullable=False, default="MAX")
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lastRunAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastRunSummary: Mapped[dict | None] = mapped_column(JSON)

    linkages: Mapped[list["RollupLinkage"]] = relationship(back_populates="rollupRule")

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RollupLinkage(Base, IdMixin):
    __tablename__ = "RollupLinkage"

    enterpriseRiskId: Mapped[str] = mapped_column(ForeignKey("EnterpriseRisk.id", ondelete="CASCADE"), nullable=False)
    enterpriseRisk: Mapped[EnterpriseRisk] = relationship(back_populates="rollupLinkages")
    rollupRuleId: Mapped[str | None] = mapped_column(ForeignKey("RollupRule.id"))
    rollupRule: Mapped["RollupRule | None"] = relationship(back_populates="linkages")
    sourceRegisterEntryId: Mapped[str] = mapped_column(String, nullable=False)
    sourceModule: Mapped[str] = mapped_column(String, nullable=False)
    sourceRef: Mapped[str | None] = mapped_column(String)
    contributingScore: Mapped[int] = mapped_column(Integer, nullable=False)
    contributingBand: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("enterpriseRiskId", "sourceRegisterEntryId", name="uq_RollupLinkage_pair"),
        Index("ix_RollupLinkage_risk", "enterpriseRiskId"),
        Index("ix_RollupLinkage_rule", "rollupRuleId"),
    )


# ─────────────────────────────────────────────────────────────────────
# Reviews
# ─────────────────────────────────────────────────────────────────────
class ReviewCycleConfig(Base, IdMixin):
    __tablename__ = "ReviewCycleConfig"

    ratingBand: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    reviewFrequencyDays: Mapped[int] = mapped_column(Integer, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class RiskReview(Base, IdMixin):
    __tablename__ = "RiskReview"

    riskId: Mapped[str] = mapped_column(ForeignKey("EnterpriseRisk.id", ondelete="CASCADE"), nullable=False)
    risk: Mapped[EnterpriseRisk] = relationship(back_populates="reviews")
    reviewDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewedBy: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    newAssessmentId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_RiskReview_risk_date", "riskId", "reviewDate"),)


# ─────────────────────────────────────────────────────────────────────
# Board pack + snapshots
# ─────────────────────────────────────────────────────────────────────
class ErmBoardPack(Base, IdMixin):
    __tablename__ = "ErmBoardPack"

    title: Mapped[str] = mapped_column(String, nullable=False)
    quarterLabel: Mapped[str] = mapped_column(String, nullable=False)
    periodStart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    periodEnd: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    sections: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    commentary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    snapshotHash: Mapped[str | None] = mapped_column(String)
    generatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publishedBy: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_ErmBoardPack_status_quarter", "status", "quarterLabel"),)


class ErmRiskSnapshot(Base, IdMixin):
    __tablename__ = "ErmRiskSnapshot"

    quarterLabel: Mapped[str] = mapped_column(String, nullable=False)
    snapshotDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    riskId: Mapped[str] = mapped_column(String, nullable=False)
    riskCode: Mapped[str] = mapped_column(String, nullable=False)
    categoryCode: Mapped[str] = mapped_column(String, nullable=False)
    inherentScore: Mapped[int | None] = mapped_column(Integer)
    inherentBand: Mapped[str | None] = mapped_column(String)
    residualScore: Mapped[int | None] = mapped_column(Integer)
    residualBand: Mapped[str | None] = mapped_column(String)
    likelihood: Mapped[int | None] = mapped_column(Integer)
    overallImpact: Mapped[int | None] = mapped_column(Integer)
    lifecycleState: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("quarterLabel", "riskId", name="uq_ErmRiskSnapshot_quarter_risk"),
        Index("ix_ErmRiskSnapshot_quarter", "quarterLabel"),
        Index("ix_ErmRiskSnapshot_risk", "riskId"),
    )


__all__ = [
    "RiskCategory",
    "RiskSubCategory",
    "ScoringMatrixConfig",
    "EnterpriseRisk",
    "RiskAssessment",
    "RiskLinkage",
    "RollupRule",
    "RollupLinkage",
    "ReviewCycleConfig",
    "RiskReview",
    "ErmBoardPack",
    "ErmRiskSnapshot",
]
