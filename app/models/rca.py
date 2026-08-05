"""ERM Cross-Domain Root Cause Analysis (RCA) & Causal Intelligence.

A single, first-class, domain-agnostic RCA store that ALL origination paths
write to and that the ERM causal-analytics layer reads from:

  - RootCauseCategory      — enterprise cause layer (~7, common to ALL domains)
  - RootCauseSubCause      — domain-scoped leaves; each rolls up to ONE category
  - RootCauseAnalysis      — the RCA (originable from an event, a risk, or a loss)
  - RcaIdentifiedCause     — a tagged cause within an RCA (the analytical payload)
  - RcaRiskLink            — RCA → risk(s) it contributes to (the "combination")

Event-derived RCAs are *exposed* from the incident (which stays system-of-record);
risk- and loss-event-derived RCAs author their analysisPayload directly. There is
NO parallel RCA store — see app/services/rca.py::expose_incident_rca.

Schema is created by safeops_360/prisma/apply-rca-ddl.ts (hand-DDL — `prisma db
push` would drop drifted tables). camelCase columns match the DB.
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

from app.models._base import Base, IdMixin, SoftDeleteMixin


# ─────────────────────────────────────────────────────────────────────
# Two-layer controlled taxonomy
# ─────────────────────────────────────────────────────────────────────
class RootCauseCategory(Base, IdMixin):
    """ENTERPRISE layer — common across ALL risk domains (~7 entries).
    Mirrors RiskCategory in app/models/erm.py."""

    __tablename__ = "RootCauseCategory"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # GOV|PROC|PEOPLE|THIRD_PARTY|TECH|EXTERNAL|DESIGN
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    colorHex: Mapped[str] = mapped_column(String, nullable=False, default="#475569")
    displayOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    subCauses: Mapped[list["RootCauseSubCause"]] = relationship(back_populates="category")

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RootCauseSubCause(Base, IdMixin):
    """DOMAIN layer — the leaves analysts pick. Domain-scoped (a financial RCA's
    picker shows hedging/concentration, never LOTO) but every leaf maps to exactly
    ONE enterprise category, which is what lets a single category light up across
    multiple domains in the rollup."""

    __tablename__ = "RootCauseSubCause"

    categoryId: Mapped[str] = mapped_column(
        ForeignKey("RootCauseCategory.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[RootCauseCategory] = relationship(back_populates="subCauses")
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # which RiskDomains may select this sub-cause (scopes the picker)
    applicableDomains: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    synonyms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # aids search + free-text migration
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_RootCauseSubCause_category_active", "categoryId", "isActive"),)


# ─────────────────────────────────────────────────────────────────────
# The RCA entity (first-class, domain-agnostic) — a GOVERNED entity
# ─────────────────────────────────────────────────────────────────────
class RootCauseAnalysis(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "RootCauseAnalysis"

    rcaCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # "RCA-2026-0042"
    title: Mapped[str] = mapped_column(String, nullable=False)
    originType: Mapped[str] = mapped_column(String, nullable=False)  # EVENT | RISK | LOSS_EVENT

    # Polymorphic origin reference — exactly one is set per originType (service guard).
    sourceEventId: Mapped[str | None] = mapped_column(String)        # Incident | AuditFinding | NearMiss
    sourceRiskId: Mapped[str | None] = mapped_column(String)         # EnterpriseRisk
    sourceLossEventId: Mapped[str | None] = mapped_column(String)    # LossEvent

    primaryDomain: Mapped[str] = mapped_column(String, nullable=False)  # OPERATIONAL|FINANCIAL|COMPLIANCE|EXTERNAL|REPUTATIONAL|CYBER|STRATEGIC|ESG
    methodology: Mapped[str] = mapped_column(String, nullable=False)    # FIVE_WHY|FISHBONE|FTA|BOWTIE|TAPROOT|CAUSE_MAP|NARRATIVE
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")  # DRAFT|IN_ANALYSIS|PEER_REVIEW|APPROVED|SUPERSEDED

    analysisPayload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # methodology-specific structured tree
    narrative: Mapped[str | None] = mapped_column(Text)  # executive summary of the causal story

    analystId: Mapped[str] = mapped_column(String, nullable=False)
    approverId: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrenceDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # when the underlying event/loss occurred

    plantId: Mapped[str | None] = mapped_column(String)
    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default", index=True)

    identifiedCauses: Mapped[list["RcaIdentifiedCause"]] = relationship(
        back_populates="rca", cascade="all, delete-orphan"
    )
    riskLinks: Mapped[list["RcaRiskLink"]] = relationship(
        back_populates="rca", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedBy: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index("ix_RootCauseAnalysis_tenant_status", "tenantId", "status"),
        Index("ix_RootCauseAnalysis_origin", "originType"),
        Index("ix_RootCauseAnalysis_domain", "primaryDomain"),
        Index("ix_RootCauseAnalysis_source_risk", "sourceRiskId"),
        Index("ix_RootCauseAnalysis_source_loss", "sourceLossEventId"),
        Index("ix_RootCauseAnalysis_source_event", "sourceEventId"),
        Index("ix_RootCauseAnalysis_occurrence", "occurrenceDate"),
        Index("ix_RootCauseAnalysis_deleted_status", "isDeleted", "status"),
    )


# ─────────────────────────────────────────────────────────────────────
# A tagged cause within an RCA — the analytical payload
# ─────────────────────────────────────────────────────────────────────
class RcaIdentifiedCause(Base, IdMixin):
    __tablename__ = "RcaIdentifiedCause"

    rcaId: Mapped[str] = mapped_column(
        ForeignKey("RootCauseAnalysis.id", ondelete="CASCADE"), nullable=False
    )
    rca: Mapped[RootCauseAnalysis] = relationship(back_populates="identifiedCauses")
    subCauseId: Mapped[str] = mapped_column(ForeignKey("RootCauseSubCause.id"), nullable=False)
    # denormalised from subCause for fast rollup (always == subCause.categoryId)
    enterpriseCategoryId: Mapped[str] = mapped_column(ForeignKey("RootCauseCategory.id"), nullable=False)
    causalRole: Mapped[str] = mapped_column(String, nullable=False, default="CONTRIBUTING")  # ROOT|CONTRIBUTING|DIRECT
    description: Mapped[str | None] = mapped_column(Text)  # free-text colour ON TOP of the structured tag
    confidence: Mapped[str | None] = mapped_column(String)  # CONFIRMED|PROBABLE|POSSIBLE
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_RcaIdentifiedCause_rca", "rcaId"),
        Index("ix_RcaIdentifiedCause_subcause", "subCauseId"),
        Index("ix_RcaIdentifiedCause_category", "enterpriseCategoryId"),
    )


# ─────────────────────────────────────────────────────────────────────
# Link from an RCA to the risk(s) it contributes to (1..n — the "combination")
# ─────────────────────────────────────────────────────────────────────
class RcaRiskLink(Base, IdMixin):
    __tablename__ = "RcaRiskLink"

    rcaId: Mapped[str] = mapped_column(
        ForeignKey("RootCauseAnalysis.id", ondelete="CASCADE"), nullable=False
    )
    rca: Mapped[RootCauseAnalysis] = relationship(back_populates="riskLinks")
    riskId: Mapped[str] = mapped_column(ForeignKey("EnterpriseRisk.id", ondelete="CASCADE"), nullable=False)
    # CAUSED = materialised the risk; ELEVATED = pushed residual up;
    # REVEALED = exposed a previously-unscored risk; RECURRING_DRIVER = repeat contributor
    contributionType: Mapped[str] = mapped_column(String, nullable=False, default="CAUSED")
    weight: Mapped[float | None] = mapped_column(Float)  # 0..1 — analyst's view of how much this cause drives this risk
    note: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_RcaRiskLink_rca", "rcaId"),
        Index("ix_RcaRiskLink_risk", "riskId"),
    )


__all__ = [
    "RootCauseCategory",
    "RootCauseSubCause",
    "RootCauseAnalysis",
    "RcaIdentifiedCause",
    "RcaRiskLink",
]
