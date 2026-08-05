"""Fire Safety & Emergency Response (P1-4).

Equipment lifecycle, assembly points, emergency plans, drills, and the
incident→crisis link. Fire equipment INSPECTIONS are not stored here — they are
CAMS engagements (sourceModule='FIRE', sourceEntityId=equipment.id): one engine,
no parallel checklist store. Emergency plans optionally link to a BCM
ContinuityPlan; a CRITICAL fire incident escalates to an ERM-P3 CrisisEvent.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, SoftDeleteMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


class FireEquipment(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireEquipment"
    equipmentCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    make: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    serialNo: Mapped[str | None] = mapped_column(String)
    location: Mapped[str] = mapped_column(String, nullable=False)
    buildingId: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(String, nullable=False)  # == siteId
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    floorLevel: Mapped[int | None] = mapped_column(Integer)
    installationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastInspectionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextInspectionDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inspectionFrequencyDays: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")  # computed
    capacitySpec: Mapped[str | None] = mapped_column(String)
    maintenanceContractor: Mapped[str | None] = mapped_column(String)
    qrCode: Mapped[str | None] = mapped_column(String)
    outOfServiceReason: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        Index("ix_FireEquipment_plant_status", "plantId", "status"),
        Index("ix_FireEquipment_type", "type"),
        Index("ix_FireEquipment_due", "nextInspectionDueDate"),
    )


class AssemblyPoint(Base, IdMixin):
    __tablename__ = "AssemblyPoint"
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    buildingIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    capacity: Mapped[int | None] = mapped_column(Integer)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    wardenUserId: Mapped[str | None] = mapped_column(String)
    alternateWardenUserId: Mapped[str | None] = mapped_column(String)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_AssemblyPoint_plant", "plantId"),)


class FireEmergencyPlan(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireEmergencyPlan"
    planCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    continuityPlanId: Mapped[str | None] = mapped_column(String)  # BCM ContinuityPlan link
    fireTypes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    commandStructure: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    callTree: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    assemblyPointIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    criticalEquipmentShutdownSequence: Mapped[str | None] = mapped_column(Text)
    hazmatLocations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    externalContacts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    lastReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_FireEmergencyPlan_plant", "plantId"),)


class FireDrill(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireDrill"
    drillCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    drillType: Mapped[str] = mapped_column(String, nullable=False)
    planId: Mapped[str | None] = mapped_column(String)
    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conductedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False, default="PLANNED")
    outcome: Mapped[str | None] = mapped_column(String)
    facilitatorId: Mapped[str | None] = mapped_column(String)
    participantCount: Mapped[int | None] = mapped_column(Integer)
    evacuationTimeMinutes: Mapped[float | None] = mapped_column(Float)
    evacuationTargetMinutes: Mapped[float | None] = mapped_column(Float)
    assemblyPointVerified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unaccountedPersons: Mapped[int | None] = mapped_column(Integer)
    reportRichText: Mapped[str | None] = mapped_column(Text)
    isAnnualMandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_FireDrill_plant_status", "plantId", "status"),)


class FireDrillFinding(Base, IdMixin):
    __tablename__ = "FireDrillFinding"
    drillId: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)  # OBSERVATION|MINOR_GAP|MAJOR_GAP
    description: Mapped[str] = mapped_column(Text, nullable=False)
    capaId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    __table_args__ = (Index("ix_FireDrillFinding_drill", "drillId"),)


class FireIncidentLink(Base, IdMixin):
    __tablename__ = "FireIncidentLink"
    incidentId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str | None] = mapped_column(String)
    affectedEquipmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    crisisEventId: Mapped[str | None] = mapped_column(String)
    evacuationOrdered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fireServiceCalled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    estimatedPropertyDamageInr: Mapped[float | None] = mapped_column(Float)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_FireIncidentLink_incident", "incidentId"),)


__all__ = [
    "FireEquipment", "AssemblyPoint", "FireEmergencyPlan",
    "FireDrill", "FireDrillFinding", "FireIncidentLink",
]
