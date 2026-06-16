"""PPE Management (PPE-01).

SQLAlchemy mirror of the Prisma `PpeType` / `PpeItem` / `PpeIssuance` /
`PpeInspection` / `PpeRequirementProfile` / `PpeBatch` models. camelCase
columns match Prisma exactly. PPE is the last tier of the control hierarchy:
this module is the authoritative record of what PPE exists, who holds it,
whether it is serviceable, and whether each person has the right PPE for the
hazard. Items move through a lifecycle (commission → issue → return → inspect
→ retire); issuance + inspection records are append-only audit trails. See the
PPE Management Build Prompt §3.
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


class PpeType(Base, IdMixin):
    __tablename__ = "PpeType"

    tenantId: Mapped[str | None] = mapped_column(String)
    code: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False, default="")
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    subcategory: Mapped[str] = mapped_column(String, nullable=False, default="")
    applicableStandards: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    minimumSpecification: Mapped[str] = mapped_column(String, nullable=False, default="")
    acceptableBrandsOrModels: Mapped[str] = mapped_column(String, nullable=False, default="")
    controlsHazards: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    enablesPermitTypes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requiredForAreas: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    serviceLifeYears: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    serviceLifeHours: Mapped[int | None] = mapped_column(Integer)
    inspectionSchedule: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requiresCompetencyToUse: Mapped[str | None] = mapped_column(String)
    requiresFitTest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fitTestValidityMonths: Mapped[int | None] = mapped_column(Integer)
    requiredTrainingPrograms: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    tracksIndividualItems: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reorderPointPer100Workers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isPersonalIssue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    statutoryProvisionRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    regulatoryReferences: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isGlobal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    items: Mapped[list["PpeItem"]] = relationship(back_populates="ppeType")


class PpeItem(Base, IdMixin):
    __tablename__ = "PpeItem"

    tenantId: Mapped[str | None] = mapped_column(String)
    itemNumber: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    serialNumber: Mapped[str] = mapped_column(String, nullable=False)
    ppeTypeId: Mapped[str] = mapped_column(
        ForeignKey("PpeType.id"), nullable=False, index=True
    )
    ppeType: Mapped[PpeType] = relationship(back_populates="items")
    ppeTypeCode: Mapped[str] = mapped_column(String, nullable=False)
    ppeTypeName: Mapped[str] = mapped_column(String, nullable=False)
    manufacturer: Mapped[str] = mapped_column(String, nullable=False, default="")
    model: Mapped[str] = mapped_column(String, nullable=False, default="")
    batchLotNumber: Mapped[str] = mapped_column(String, nullable=False, default="")
    manufactureDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purchaseDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purchaseOrderReference: Mapped[str | None] = mapped_column(String)
    cost: Mapped[float | None] = mapped_column(Float)
    costCurrency: Mapped[str] = mapped_column(String, nullable=False, default="INR")
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    departmentId: Mapped[str | None] = mapped_column(String)
    storageLocation: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="in_stock")
    currentIssuanceId: Mapped[str | None] = mapped_column(String)
    currentHolderUserId: Mapped[str | None] = mapped_column(String)
    issuedSince: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    condition: Mapped[str] = mapped_column(String, nullable=False, default="new")
    lastConditionUpdateAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastConditionUpdateByUserId: Mapped[str | None] = mapped_column(String)
    commissionedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    serviceLifeEndDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    serviceHoursUsed: Mapped[int | None] = mapped_column(Integer)
    lastInspectedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastInspectedByUserId: Mapped[str | None] = mapped_column(String)
    nextInspectionDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastFitTestedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fitTestValidUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fitTestHolderUserId: Mapped[str | None] = mapped_column(String)
    batchUnderRecall: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recallReference: Mapped[str | None] = mapped_column(String)
    recallIssuedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stateHistory: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    versionNumber: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class PpeIssuance(Base, IdMixin):
    __tablename__ = "PpeIssuance"

    tenantId: Mapped[str | None] = mapped_column(String)
    issuanceNumber: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    ppeItemId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ppeTypeCode: Mapped[str] = mapped_column(String, nullable=False)
    ppeTypeName: Mapped[str] = mapped_column(String, nullable=False)
    serialNumber: Mapped[str] = mapped_column(String, nullable=False)
    issuedToUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    issuedToName: Mapped[str] = mapped_column(String, nullable=False)
    issuedToDepartment: Mapped[str] = mapped_column(String, nullable=False, default="")
    issuedToRole: Mapped[str] = mapped_column(String, nullable=False, default="")
    issuedByUserId: Mapped[str] = mapped_column(String, nullable=False)
    issuedByName: Mapped[str] = mapped_column(String, nullable=False)
    issuedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expectedReturnDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issuancePurpose: Mapped[str] = mapped_column(String, nullable=False, default="personal_assignment")
    linkedPermitId: Mapped[str | None] = mapped_column(String)
    linkedWorkOrder: Mapped[str | None] = mapped_column(String)
    conditionAtIssuance: Mapped[str] = mapped_column(String, nullable=False, default="good")
    conditionNotesAtIssuance: Mapped[str] = mapped_column(String, nullable=False, default="")
    preIssuanceInspectionDone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    preIssuanceInspectorUserId: Mapped[str | None] = mapped_column(String)
    recipientAcknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recipientAcknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipientSignatureUrl: Mapped[str | None] = mapped_column(String)
    briefingProvided: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    briefingByUserId: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    returnedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returnedByUserId: Mapped[str | None] = mapped_column(String)
    conditionAtReturn: Mapped[str | None] = mapped_column(String)
    conditionNotesAtReturn: Mapped[str] = mapped_column(String, nullable=False, default="")
    postReturnInspectionRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class PpeInspection(Base, IdMixin):
    __tablename__ = "PpeInspection"

    tenantId: Mapped[str | None] = mapped_column(String)
    ppeItemId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ppeTypeCode: Mapped[str] = mapped_column(String, nullable=False)
    serialNumber: Mapped[str] = mapped_column(String, nullable=False)
    inspectionType: Mapped[str] = mapped_column(String, nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False)
    linkedPermitId: Mapped[str | None] = mapped_column(String)
    linkedIncidentId: Mapped[str | None] = mapped_column(String)
    scheduledDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conductedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    inspectorUserId: Mapped[str] = mapped_column(String, nullable=False)
    inspectorName: Mapped[str] = mapped_column(String, nullable=False)
    inspectorQualification: Mapped[str] = mapped_column(String, nullable=False, default="")
    isThirdPartyInspection: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    thirdPartyCompany: Mapped[str] = mapped_column(String, nullable=False, default="")
    thirdPartyCertificateReference: Mapped[str] = mapped_column(String, nullable=False, default="")
    checklistTemplateId: Mapped[str | None] = mapped_column(String)
    checklistItems: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    overallResult: Mapped[str] = mapped_column(String, nullable=False)
    defectsFound: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    conditions: Mapped[str] = mapped_column(String, nullable=False, default="")
    reInspectionRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reInspectionDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    itemStatusAfterInspection: Mapped[str] = mapped_column(String, nullable=False)
    serviceLifeRemainingDays: Mapped[int | None] = mapped_column(Integer)
    inspectionCertificateUrl: Mapped[str | None] = mapped_column(String)
    certificateValidUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capaSpawned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capaId: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class PpeRequirementProfile(Base, IdMixin):
    __tablename__ = "PpeRequirementProfile"
    __table_args__ = (
        UniqueConstraint(
            "plantId", "scopeType", "scopeId",
            name="PpeRequirementProfile_plantId_scopeType_scopeId_key",
        ),
    )

    tenantId: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    scopeType: Mapped[str] = mapped_column(String, nullable=False)
    scopeId: Mapped[str] = mapped_column(String, nullable=False)
    scopeName: Mapped[str] = mapped_column(String, nullable=False)
    requiredPpe: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    effectiveFrom: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    supersededAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedByUserId: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


class PpeBatch(Base, IdMixin):
    __tablename__ = "PpeBatch"

    tenantId: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ppeTypeId: Mapped[str] = mapped_column(String, nullable=False)
    batchLotNumber: Mapped[str] = mapped_column(String, nullable=False)
    manufacturer: Mapped[str] = mapped_column(String, nullable=False, default="")
    manufactureDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purchaseDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    itemsInBatch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    underRecall: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recallReason: Mapped[str] = mapped_column(String, nullable=False, default="")
    recallIssuedBy: Mapped[str] = mapped_column(String, nullable=False, default="")
    recallIssuedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recallActionRequired: Mapped[str] = mapped_column(String, nullable=False, default="")
    recallResolvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "PpeType",
    "PpeItem",
    "PpeIssuance",
    "PpeInspection",
    "PpeRequirementProfile",
    "PpeBatch",
]
