from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class FLRAStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class FLRA(Base, IdMixin):
    __tablename__ = "FLRA"
    __table_args__ = (Index("ix_flra_permit_status", "permitId", "status"),)

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    permitId: Mapped[str | None] = mapped_column(ForeignKey("Permit.id"))
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    jobDescription: Mapped[str] = mapped_column(Text, nullable=False)
    leaderId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    hazards: Mapped[str] = mapped_column(Text, nullable=False)  # legacy JSON string
    toolboxTalkById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    toolboxTalkConfirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[FLRAStatus] = mapped_column(
        Enum(FLRAStatus, name="FLRAStatus", native_enum=False),
        nullable=False,
        default=FLRAStatus.IN_PROGRESS,
    )
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersededById: Mapped[str | None] = mapped_column(ForeignKey("FLRA.id"))
    supersededReason: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    # ─── PTW + FLRA refactor — Commit 1 additions ───
    isStandalone: Mapped[bool] = mapped_column(Boolean, default=False)

    departmentId: Mapped[str | None] = mapped_column(ForeignKey("Department.id"))
    areaCode: Mapped[str | None] = mapped_column(String)
    specificLocation: Mapped[str | None] = mapped_column(Text)
    gpsLatitude: Mapped[float | None] = mapped_column(Float)
    gpsLongitude: Mapped[float | None] = mapped_column(Float)
    startTime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    jobIsRoutine: Mapped[bool | None] = mapped_column(Boolean)

    toolboxTalkConducted: Mapped[bool] = mapped_column(Boolean, default=False)
    toolboxTalkConductedAt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    toolboxTalkTopics: Mapped[Any | None] = mapped_column(JSON)
    toolboxTalkLanguage: Mapped[str | None] = mapped_column(String)

    ppeChecklistResponses: Mapped[Any | None] = mapped_column(JSON)
    toolsCheckedResponses: Mapped[Any | None] = mapped_column(JSON)
    exitRoutesIdentified: Mapped[str | None] = mapped_column(Text)
    emergencyContactsConfirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    teamMembers: Mapped[list[FLRATeamMember]] = relationship(
        back_populates="flra", cascade="all, delete-orphan"
    )
    crewSignatures: Mapped[list[FLRACrewSignature]] = relationship(
        back_populates="flra", cascade="all, delete-orphan"
    )
    jobSteps: Mapped[list[FLRAJobStep]] = relationship(
        back_populates="flra", cascade="all, delete-orphan"
    )
    fitnessDeclarations: Mapped[list[FLRAFitnessDeclaration]] = relationship(
        back_populates="flra", cascade="all, delete-orphan"
    )
    attachments: Mapped[list[FLRAAttachment]] = relationship(
        back_populates="flra", cascade="all, delete-orphan"
    )


class FLRATeamMember(Base, IdMixin):
    __tablename__ = "FLRATeamMember"
    __table_args__ = (UniqueConstraint("flraId", "userId", name="uq_flra_team"),)

    flraId: Mapped[str] = mapped_column(
        ForeignKey("FLRA.id", ondelete="CASCADE"), nullable=False
    )
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    flra: Mapped[FLRA] = relationship(back_populates="teamMembers")


class FLRACrewSignature(Base, IdMixin):
    __tablename__ = "FLRACrewSignature"
    __table_args__ = (UniqueConstraint("flraId", "userId", name="uq_flra_signature"),)

    flraId: Mapped[str] = mapped_column(
        ForeignKey("FLRA.id", ondelete="CASCADE"), nullable=False
    )
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    signed: Mapped[bool] = mapped_column(Boolean, default=False)
    signedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ipAddress: Mapped[str | None] = mapped_column(String)
    deviceInfo: Mapped[str | None] = mapped_column(String)
    trainingValidAtSignature: Mapped[bool] = mapped_column(Boolean, default=True)
    trainingExpiresAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ─── Refusal handling ───
    refusedToSign: Mapped[bool] = mapped_column(Boolean, default=False)
    refusalReason: Mapped[str | None] = mapped_column(Text)
    refusalEscalatedToId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    refusalEscalatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    flra: Mapped[FLRA] = relationship(back_populates="crewSignatures")


# ─── FLRA child models — Commit 1 additions ───────────────────────────


class FLRAJobStep(Base, IdMixin):
    __tablename__ = "FLRAJobStep"
    __table_args__ = (Index("ix_flra_jobstep_seq", "flraId", "sequence"),)

    flraId: Mapped[str] = mapped_column(
        ForeignKey("FLRA.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stepDescription: Mapped[str] = mapped_column(Text, nullable=False)

    flra: Mapped[FLRA] = relationship(back_populates="jobSteps")
    hazards: Mapped[list[FLRAStepHazard]] = relationship(
        back_populates="jobStep", cascade="all, delete-orphan"
    )


class FLRAStepHazard(Base, IdMixin):
    __tablename__ = "FLRAStepHazard"
    __table_args__ = (Index("ix_flra_hazard_step", "jobStepId"),)

    jobStepId: Mapped[str] = mapped_column(
        ForeignKey("FLRAJobStep.id", ondelete="CASCADE"), nullable=False
    )

    hazardDescription: Mapped[str] = mapped_column(Text, nullable=False)
    hazardCategory: Mapped[str] = mapped_column(String, nullable=False)
    energySource: Mapped[str | None] = mapped_column(String)

    initialLikelihood: Mapped[int] = mapped_column(Integer, nullable=False)
    initialSeverity: Mapped[int] = mapped_column(Integer, nullable=False)
    initialRiskScore: Mapped[int] = mapped_column(Integer, nullable=False)
    initialRiskLevel: Mapped[str] = mapped_column(String, nullable=False)

    controlMeasures: Mapped[str] = mapped_column(Text, nullable=False)

    residualLikelihood: Mapped[int] = mapped_column(Integer, nullable=False)
    residualSeverity: Mapped[int] = mapped_column(Integer, nullable=False)
    residualRiskScore: Mapped[int] = mapped_column(Integer, nullable=False)
    residualRiskLevel: Mapped[str] = mapped_column(String, nullable=False)

    jobStep: Mapped[FLRAJobStep] = relationship(back_populates="hazards")


class FLRAFitnessDeclaration(Base, IdMixin):
    __tablename__ = "FLRAFitnessDeclaration"
    __table_args__ = (
        UniqueConstraint("flraId", "userId", name="uq_flra_fitness"),
        Index("ix_flra_fitness_user", "userId"),
    )

    flraId: Mapped[str] = mapped_column(
        ForeignKey("FLRA.id", ondelete="CASCADE"), nullable=False
    )
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    isFit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    declaredAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    hasMedicalCondition: Mapped[bool] = mapped_column(Boolean, default=False)
    conditionsDeclared: Mapped[str | None] = mapped_column(Text)
    hadAdequateRest: Mapped[bool] = mapped_column(Boolean, nullable=False)
    underInfluenceCheck: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    flra: Mapped[FLRA] = relationship(back_populates="fitnessDeclarations")


class FLRAAttachment(Base, IdMixin):
    __tablename__ = "FLRAAttachment"
    __table_args__ = (Index("ix_flra_attach_cat", "flraId", "category"),)

    flraId: Mapped[str] = mapped_column(
        ForeignKey("FLRA.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    flra: Mapped[FLRA] = relationship(back_populates="attachments")
