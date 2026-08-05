"""CAPA — Corrective Action and Preventive Action (unified).

Mirrors the Prisma `Capa` family in
[safeops_360/prisma/schema.prisma](../../../safeops_360/prisma/schema.prisma)
section "CAPA — Corrective Action and Preventive Action".

Schema is owned by Prisma. This file lets SQLAlchemy-side routers read/
write the same tables. camelCase column names are required to match.

Tables in this file:
  - CapaSourceCategory, CapaSourceType, CapaSubCategory, CapaSlaProfile,
    CapaVerificationMethod — master data
  - Capa — the unified record
  - CapaAction, CapaRootCause, CapaContributor, CapaLinkage,
    CapaPatternGroup, CapaAttachment, CapaComment — children
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin, SoftDeleteMixin


# ─────────────────────────────────────────────────────────────────────
# Master data
# ─────────────────────────────────────────────────────────────────────


class CapaSourceCategory(Base, IdMixin):
    __tablename__ = "CapaSourceCategory"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    prefix: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class CapaSourceType(Base, IdMixin):
    __tablename__ = "CapaSourceType"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    categoryId: Mapped[str] = mapped_column(ForeignKey("CapaSourceCategory.id"), nullable=False, index=True)
    parentModuleLive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parentModuleName: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class CapaSubCategory(Base, IdMixin):
    __tablename__ = "CapaSubCategory"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    applicableSourceTypeIds: Mapped[list | None] = mapped_column(JSON)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class CapaSlaProfile(Base, IdMixin):
    __tablename__ = "CapaSlaProfile"
    __table_args__ = ({},)

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    sourceTypeCode: Mapped[str | None] = mapped_column(String)
    severity: Mapped[str | None] = mapped_column(String)

    initialResponseHours: Mapped[int] = mapped_column(Integer, nullable=False)
    rcaDueDays: Mapped[int] = mapped_column(Integer, nullable=False)
    actionsPlannedDueDays: Mapped[int] = mapped_column(Integer, nullable=False)
    closureTargetDays: Mapped[int] = mapped_column(Integer, nullable=False)
    recurrenceCheckDays: Mapped[int] = mapped_column(Integer, nullable=False, default=90)

    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class CapaVerificationMethod(Base, IdMixin):
    __tablename__ = "CapaVerificationMethod"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────
# Capa — main record
# ─────────────────────────────────────────────────────────────────────


class Capa(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "Capa"

    capaNumber: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    aliasNumber: Mapped[str | None] = mapped_column(String, unique=True)
    legacySource: Mapped[str | None] = mapped_column(String)
    legacyId: Mapped[str | None] = mapped_column(String, unique=True)

    title: Mapped[str] = mapped_column(String, nullable=False)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)

    # Source
    sourceCategoryId: Mapped[str] = mapped_column(ForeignKey("CapaSourceCategory.id"), nullable=False)
    sourceTypeId: Mapped[str] = mapped_column(ForeignKey("CapaSourceType.id"), nullable=False)
    sourceTypeCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sourceReferenceId: Mapped[str | None] = mapped_column(String)
    sourceReferenceUrl: Mapped[str | None] = mapped_column(String)
    sourceReferenceSummary: Mapped[str | None] = mapped_column(Text)
    sourceMetadata: Mapped[dict | None] = mapped_column(JSON)

    # Problem
    problemDescription: Mapped[str] = mapped_column(Text, nullable=False)
    problemImpact: Mapped[str | None] = mapped_column(Text)
    detectionMethod: Mapped[str | None] = mapped_column(String)
    detectedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detectedByUserId: Mapped[str | None] = mapped_column(String)
    affectedAreas: Mapped[list | None] = mapped_column(JSON)
    affectedDepartments: Mapped[list | None] = mapped_column(JSON)
    affectedProducts: Mapped[list | None] = mapped_column(JSON)
    affectedProcesses: Mapped[list | None] = mapped_column(JSON)
    affectedCustomers: Mapped[list | None] = mapped_column(JSON)

    # Classification
    primaryCategory: Mapped[str] = mapped_column(String, nullable=False)
    subCategoryId: Mapped[str | None] = mapped_column(ForeignKey("CapaSubCategory.id"))
    actionType: Mapped[str] = mapped_column(String, nullable=False, default="CORRECTIVE_AND_PREVENTIVE")
    severity: Mapped[str] = mapped_column(String, nullable=False, default="MODERATE")
    priority: Mapped[str] = mapped_column(String, nullable=False, default="MODERATE")
    isRecurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    relatedCapaIds: Mapped[list | None] = mapped_column(JSON)

    # RCA
    rcaMethodology: Mapped[str | None] = mapped_column(String)
    rcaMethodologyRationale: Mapped[str | None] = mapped_column(Text)
    rcaCompleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rcaRecordId: Mapped[str | None] = mapped_column(String)
    rcaSummary: Mapped[str | None] = mapped_column(Text)
    contributingFactors: Mapped[list | None] = mapped_column(JSON)
    rcaCompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rcaCompletedByUserId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    # Verification
    verificationMethodId: Mapped[str | None] = mapped_column(ForeignKey("CapaVerificationMethod.id"))
    verificationSuccessCriteria: Mapped[str | None] = mapped_column(Text)
    measurementPeriodDays: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    verificationDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verificationCompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verificationCompletedByUserId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    verificationResult: Mapped[str | None] = mapped_column(String)
    verificationEvidence: Mapped[str | None] = mapped_column(Text)

    recurrenceCheckDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recurrenceCheckCompletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recurrenceDetected: Mapped[bool | None] = mapped_column(Boolean)

    # Workflow state — superset enum per D1
    state: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)
    stateChangedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    stateChangedByUserId: Mapped[str | None] = mapped_column(String)

    rcaDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correctiveActionDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preventiveActionDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closureTargetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    slaBreaches: Mapped[list | None] = mapped_column(JSON)

    # Ownership
    raisedByUserId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    raisedByRole: Mapped[str | None] = mapped_column(String)
    primaryOwnerUserId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    primaryOwnerRole: Mapped[str | None] = mapped_column(String)
    departmentOwnerId: Mapped[str | None] = mapped_column(String)

    # Cost
    estimatedProblemCost: Mapped[float | None] = mapped_column(Float)
    estimatedProblemCurrency: Mapped[str | None] = mapped_column(String, default="INR")
    estimatedActionsCost: Mapped[float | None] = mapped_column(Float)
    estimatedActionsCurrency: Mapped[str | None] = mapped_column(String, default="INR")
    actualCost: Mapped[float | None] = mapped_column(Float)
    actualCostCurrency: Mapped[str | None] = mapped_column(String, default="INR")
    costCategories: Mapped[list | None] = mapped_column(JSON)

    # Audit
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdByUserId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    updatedByUserId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    versionNumber: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    closedByUserId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    actions: Mapped[list["CapaAction"]] = relationship(back_populates="capa", cascade="all, delete-orphan")
    rootCauses: Mapped[list["CapaRootCause"]] = relationship(back_populates="capa", cascade="all, delete-orphan")
    contributors: Mapped[list["CapaContributor"]] = relationship(back_populates="capa", cascade="all, delete-orphan")
    attachments: Mapped[list["CapaAttachment"]] = relationship(back_populates="capa", cascade="all, delete-orphan")
    comments: Mapped[list["CapaComment"]] = relationship(back_populates="capa", cascade="all, delete-orphan")


class CapaAction(Base, IdMixin):
    __tablename__ = "CapaAction"

    capaId: Mapped[str] = mapped_column(ForeignKey("Capa.id", ondelete="CASCADE"), nullable=False, index=True)
    capa: Mapped[Capa] = relationship(back_populates="actions")

    actionType: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)

    ownerUserId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    ownerRole: Mapped[str | None] = mapped_column(String)

    dueDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    startedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, nullable=False, default="PROPOSED")
    evidenceOfCompletion: Mapped[str | None] = mapped_column(Text)
    attachmentIds: Mapped[list | None] = mapped_column(JSON)

    costEstimate: Mapped[float | None] = mapped_column(Float)
    costEstimateCurrency: Mapped[str | None] = mapped_column(String, default="INR")

    approverUserId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflowTaskId: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class CapaRootCause(Base, IdMixin):
    __tablename__ = "CapaRootCause"

    capaId: Mapped[str] = mapped_column(ForeignKey("Capa.id", ondelete="CASCADE"), nullable=False, index=True)
    capa: Mapped[Capa] = relationship(back_populates="rootCauses")

    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="MEDIUM")
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CapaContributor(Base, IdMixin):
    __tablename__ = "CapaContributor"
    __table_args__ = (UniqueConstraint("capaId", "userId", "contributionType", name="CapaContributor_uniq"),)

    capaId: Mapped[str] = mapped_column(ForeignKey("Capa.id", ondelete="CASCADE"), nullable=False, index=True)
    capa: Mapped[Capa] = relationship(back_populates="contributors")
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    role: Mapped[str | None] = mapped_column(String)
    contributionType: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CapaLinkage(Base, IdMixin):
    __tablename__ = "CapaLinkage"
    __table_args__ = (UniqueConstraint("fromCapaId", "toCapaId", "linkageType", name="CapaLinkage_uniq"),)

    fromCapaId: Mapped[str] = mapped_column(ForeignKey("Capa.id", ondelete="CASCADE"), nullable=False)
    toCapaId: Mapped[str] = mapped_column(ForeignKey("Capa.id", ondelete="CASCADE"), nullable=False, index=True)
    linkageType: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdByUserId: Mapped[str | None] = mapped_column(String)


class CapaPatternGroup(Base, IdMixin):
    __tablename__ = "CapaPatternGroup"

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PROPOSED")
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    capaIds: Mapped[list] = mapped_column(JSON, nullable=False)
    reviewedByUserId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CapaAttachment(Base, IdMixin):
    __tablename__ = "CapaAttachment"

    capaId: Mapped[str] = mapped_column(ForeignKey("Capa.id", ondelete="CASCADE"), nullable=False, index=True)
    capa: Mapped[Capa] = relationship(back_populates="attachments")

    category: Mapped[str] = mapped_column(String, nullable=False)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    fileUrl: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int | None] = mapped_column(Integer)
    mimeType: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    uploadedByUserId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)


class CapaComment(Base, IdMixin):
    __tablename__ = "CapaComment"

    capaId: Mapped[str] = mapped_column(ForeignKey("Capa.id", ondelete="CASCADE"), nullable=False, index=True)
    capa: Mapped[Capa] = relationship(back_populates="comments")

    body: Mapped[str] = mapped_column(Text, nullable=False)
    authorUserId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    commentType: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVITY")
    isInternal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


__all__ = [
    "CapaSourceCategory",
    "CapaSourceType",
    "CapaSubCategory",
    "CapaSlaProfile",
    "CapaVerificationMethod",
    "Capa",
    "CapaAction",
    "CapaRootCause",
    "CapaContributor",
    "CapaLinkage",
    "CapaPatternGroup",
    "CapaAttachment",
    "CapaComment",
]
