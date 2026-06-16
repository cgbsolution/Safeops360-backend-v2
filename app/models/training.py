from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
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

from app.models._base import Base, IdMixin


# ─── TrainingProgram (production-depth) ───────────────────────────────


class TrainingProgram(Base, IdMixin):
    """SQLAlchemy mirror of Prisma TrainingProgram. Most new columns are
    nullable so legacy 10-row seed data continues to read fine."""

    __tablename__ = "TrainingProgram"

    # Identity
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    programCode: Mapped[str | None] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    programName: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)

    # Category & Type
    category: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)

    # Statutory & Regulatory
    isStatutory: Mapped[bool] = mapped_column(Boolean, default=False)
    statutoryReference: Mapped[str | None] = mapped_column(String)
    isMandatoryForRoles: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    isMandatoryForActivities: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    isMandatoryForPermitTypes: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # Delivery
    durationHours: Mapped[float] = mapped_column(Float, nullable=False, default=4)
    durationSessions: Mapped[int] = mapped_column(Integer, default=1)
    maxParticipantsPerBatch: Mapped[int] = mapped_column(Integer, default=20)
    language: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # Prerequisites
    prerequisitePrograms: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    prerequisiteRoles: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    minimumExperienceMonths: Mapped[int | None] = mapped_column(Integer)
    medicalFitnessRequired: Mapped[bool] = mapped_column(Boolean, default=False)

    # Assessment
    hasAssessment: Mapped[bool] = mapped_column(Boolean, default=False)
    assessmentType: Mapped[str | None] = mapped_column(String)
    passingScore: Mapped[int] = mapped_column(Integer, default=60)  # legacy
    passingScorePercent: Mapped[int | None] = mapped_column(Integer)
    practicalAssessmentRubric: Mapped[str | None] = mapped_column(Text)
    attemptsAllowed: Mapped[int] = mapped_column(Integer, default=3)

    # Certification
    issuesCertificate: Mapped[bool] = mapped_column(Boolean, default=True)
    certificateTemplateUrl: Mapped[str | None] = mapped_column(String)
    validityMonths: Mapped[int] = mapped_column(Integer, nullable=False, default=12)  # legacy
    certificateValidityMonths: Mapped[int | None] = mapped_column(Integer)
    certificateExpiryGracePeriodDays: Mapped[int] = mapped_column(Integer, default=30)
    refresherProgramCode: Mapped[str | None] = mapped_column(String)

    # Content
    contentOutline: Mapped[dict | None] = mapped_column(JSON)
    learningObjectives: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    # Trainer Qualifications
    approvedTrainerIds: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    externalTrainerAllowed: Mapped[bool] = mapped_column(Boolean, default=False)
    trainerQualifications: Mapped[str | None] = mapped_column(Text)

    # Evaluation
    evaluatesEffectiveness: Mapped[bool] = mapped_column(Boolean, default=True)
    effectivenessReviewMonths: Mapped[int] = mapped_column(Integer, default=3)
    feedbackQuestionnaireId: Mapped[str | None] = mapped_column(String)

    # SafeOps Gates
    blocksPtwIfMissing: Mapped[bool] = mapped_column(Boolean, default=False)
    blocksRoleAssignmentIfMissing: Mapped[bool] = mapped_column(Boolean, default=False)
    blocksContractorOnboardingIfMissing: Mapped[bool] = mapped_column(Boolean, default=False)

    # Status & Approval
    mandatory: Mapped[bool] = mapped_column(Boolean, default=False)  # legacy
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)
    effectiveFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effectiveTo: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvalStatus: Mapped[str] = mapped_column(String, default="APPROVED")
    approvedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Plant scoping
    plantId: Mapped[str | None] = mapped_column(ForeignKey("Plant.id"))

    # Metadata
    ownerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    reviewFrequencyMonths: Mapped[int] = mapped_column(Integer, default=12)
    lastReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextReviewAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    # Children
    questions: Mapped[list[TrainingProgramQuestion]] = relationship(
        back_populates="program", cascade="all, delete-orphan"
    )
    materials: Mapped[list[TrainingProgramMaterial]] = relationship(
        back_populates="program", cascade="all, delete-orphan"
    )


class TrainingProgramQuestion(Base, IdMixin):
    __tablename__ = "TrainingProgramQuestion"

    programId: Mapped[str] = mapped_column(
        ForeignKey("TrainingProgram.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    questionText: Mapped[str] = mapped_column(Text, nullable=False)
    questionType: Mapped[str] = mapped_column(String, nullable=False)
    options: Mapped[list | None] = mapped_column(JSON)
    correctAnswer: Mapped[str | None] = mapped_column(String)
    marks: Mapped[int] = mapped_column(Integer, default=1)
    isCritical: Mapped[bool] = mapped_column(Boolean, default=False)
    explanation: Mapped[str | None] = mapped_column(Text)

    program: Mapped[TrainingProgram] = relationship(back_populates="questions")


class TrainingProgramMaterial(Base, IdMixin):
    __tablename__ = "TrainingProgramMaterial"

    programId: Mapped[str] = mapped_column(
        ForeignKey("TrainingProgram.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    fileUrl: Mapped[str | None] = mapped_column(String)
    externalUrl: Mapped[str | None] = mapped_column(String)
    fileSize: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String)
    isMandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)

    program: Mapped[TrainingProgram] = relationship(back_populates="materials")


# ─── Schedule + Session ──────────────────────────────────────────────


class TrainingSchedule(Base, IdMixin):
    __tablename__ = "TrainingSchedule"

    scheduleNumber: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    programId: Mapped[str] = mapped_column(
        ForeignKey("TrainingProgram.id"), nullable=False
    )
    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)

    startDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    venue: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False)

    trainerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    isExternalTrainer: Mapped[bool] = mapped_column(Boolean, default=False)
    externalTrainerName: Mapped[str | None] = mapped_column(String)
    externalTrainerOrg: Mapped[str | None] = mapped_column(String)
    externalTrainerCert: Mapped[str | None] = mapped_column(String)

    maxParticipants: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    publishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelledAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellationReason: Mapped[str | None] = mapped_column(String)

    trainerEffectivenessScore: Mapped[float | None] = mapped_column(Float)
    participantSatisfaction: Mapped[float | None] = mapped_column(Float)
    immediateAssessmentPassRate: Mapped[float | None] = mapped_column(Float)

    createdById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    approvedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    sessions: Mapped[list[TrainingSession]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )
    registrations: Mapped[list[TrainingRegistration]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )


class TrainingSession(Base, IdMixin):
    __tablename__ = "TrainingSession"

    scheduleId: Mapped[str] = mapped_column(
        ForeignKey("TrainingSchedule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    startTime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endTime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trainerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    topicsCovered: Mapped[list | None] = mapped_column(JSON)
    conductedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    durationMinutesActual: Mapped[int | None] = mapped_column(Integer)

    schedule: Mapped[TrainingSchedule] = relationship(back_populates="sessions")
    attendances: Mapped[list[TrainingAttendance]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


# ─── Registration + Attendance + Assessment ──────────────────────────


class TrainingRegistration(Base, IdMixin):
    __tablename__ = "TrainingRegistration"
    __table_args__ = (
        UniqueConstraint("scheduleId", "userId", name="uq_registration_schedule_user"),
    )

    scheduleId: Mapped[str] = mapped_column(
        ForeignKey("TrainingSchedule.id", ondelete="CASCADE"), nullable=False
    )
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)

    registrationType: Mapped[str] = mapped_column(String, nullable=False)
    nominatedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    triggerReason: Mapped[str | None] = mapped_column(String)
    triggerSourceId: Mapped[str | None] = mapped_column(String)

    prerequisitesMet: Mapped[bool] = mapped_column(Boolean, default=False)
    prerequisiteCheckResult: Mapped[dict | None] = mapped_column(JSON)

    approvalStatus: Mapped[str] = mapped_column(String, default="NOT_REQUIRED")
    approvedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, default="REGISTERED")

    attendancePercent: Mapped[float | None] = mapped_column(Float)
    assessmentScore: Mapped[float | None] = mapped_column(Float)
    assessmentAttempts: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool | None] = mapped_column(Boolean)

    certificateId: Mapped[str | None] = mapped_column(
        ForeignKey("TrainingCertificate.id"), unique=True
    )

    registeredAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    schedule: Mapped[TrainingSchedule] = relationship(back_populates="registrations")
    attendances: Mapped[list[TrainingAttendance]] = relationship(
        back_populates="registration", cascade="all, delete-orphan"
    )
    assessments: Mapped[list[TrainingAssessment]] = relationship(
        back_populates="registration", cascade="all, delete-orphan"
    )


class TrainingAttendance(Base, IdMixin):
    __tablename__ = "TrainingAttendance"
    __table_args__ = (
        UniqueConstraint("sessionId", "registrationId", name="uq_attendance_session_reg"),
    )

    sessionId: Mapped[str] = mapped_column(
        ForeignKey("TrainingSession.id", ondelete="CASCADE"), nullable=False
    )
    registrationId: Mapped[str] = mapped_column(
        ForeignKey("TrainingRegistration.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[str] = mapped_column(String, nullable=False)
    arrivalTime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    departureTime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    durationMinutes: Mapped[int | None] = mapped_column(Integer)

    signatureCaptured: Mapped[bool] = mapped_column(Boolean, default=False)
    signatureUrl: Mapped[str | None] = mapped_column(String)
    qrScanned: Mapped[bool] = mapped_column(Boolean, default=False)
    qrScannedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    geoLocation: Mapped[dict | None] = mapped_column(JSON)
    attendancePhotos: Mapped[list | None] = mapped_column(JSON)

    notes: Mapped[str | None] = mapped_column(String)

    capturedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    capturedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    session: Mapped[TrainingSession] = relationship(back_populates="attendances")
    registration: Mapped[TrainingRegistration] = relationship(back_populates="attendances")


class TrainingAssessment(Base, IdMixin):
    __tablename__ = "TrainingAssessment"
    __table_args__ = (
        UniqueConstraint(
            "registrationId", "attemptNumber", name="uq_assessment_reg_attempt"
        ),
    )

    registrationId: Mapped[str] = mapped_column(
        ForeignKey("TrainingRegistration.id", ondelete="CASCADE"), nullable=False
    )
    attemptNumber: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    startedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    durationMinutes: Mapped[int | None] = mapped_column(Integer)

    practicalScores: Mapped[dict | None] = mapped_column(JSON)
    practicalNotes: Mapped[str | None] = mapped_column(String)
    assessorNarrative: Mapped[str | None] = mapped_column(Text)

    totalScore: Mapped[float | None] = mapped_column(Float)
    totalMarks: Mapped[float] = mapped_column(Float, nullable=False)
    scorePercent: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failureReasons: Mapped[list[str] | None] = mapped_column(ARRAY(String))

    assessedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    remediationRequired: Mapped[bool] = mapped_column(Boolean, default=False)
    remediationPlan: Mapped[str | None] = mapped_column(Text)
    retakeAllowed: Mapped[bool] = mapped_column(Boolean, default=True)
    retakeAfterDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    registration: Mapped[TrainingRegistration] = relationship(back_populates="assessments")
    responses: Mapped[list[TrainingAssessmentResponse]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )


class TrainingAssessmentResponse(Base, IdMixin):
    __tablename__ = "TrainingAssessmentResponse"

    assessmentId: Mapped[str] = mapped_column(
        ForeignKey("TrainingAssessment.id", ondelete="CASCADE"), nullable=False
    )
    questionId: Mapped[str] = mapped_column(
        ForeignKey("TrainingProgramQuestion.id"), nullable=False
    )

    selectedOptions: Mapped[list | None] = mapped_column(JSON)
    textAnswer: Mapped[str | None] = mapped_column(String)
    numericAnswer: Mapped[float | None] = mapped_column(Float)

    isCorrect: Mapped[bool] = mapped_column(Boolean, nullable=False)
    marksAwarded: Mapped[float] = mapped_column(Float, nullable=False)

    assessment: Mapped[TrainingAssessment] = relationship(back_populates="responses")


# ─── Certificate (state machine + renewal + revocation + effectiveness) ─


class TrainingCertificate(Base, IdMixin):
    __tablename__ = "TrainingCertificate"

    certificateNumber: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    programId: Mapped[str] = mapped_column(
        ForeignKey("TrainingProgram.id"), nullable=False, index=True
    )
    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    registrationId: Mapped[str | None] = mapped_column(String, unique=True)

    issuedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    issuedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    finalAssessmentScore: Mapped[float | None] = mapped_column(Float)
    attendancePercent: Mapped[float | None] = mapped_column(Float)

    validFrom: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validTo: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")

    isRenewable: Mapped[bool] = mapped_column(Boolean, default=True)
    renewedFromCertificateId: Mapped[str | None] = mapped_column(
        ForeignKey("TrainingCertificate.id"), unique=True
    )

    firstExpiryReminderSent: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secondExpiryReminderSent: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    thirdExpiryReminderSent: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalExpiryReminderSent: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresherScheduledForUserAt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    revokedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revokedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    revocationReason: Mapped[str | None] = mapped_column(String)
    revocationDetails: Mapped[str | None] = mapped_column(Text)

    certificatePdfUrl: Mapped[str | None] = mapped_column(String)
    certificateQrCode: Mapped[str | None] = mapped_column(String)
    digitalSignature: Mapped[str | None] = mapped_column(String)

    effectivenessReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effectivenessReviewedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    effectivenessRating: Mapped[int | None] = mapped_column(Integer)
    effectivenessNotes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


# ─── Legacy ──────────────────────────────────────────────────────────


class TrainingRecord(Base, IdMixin):
    """Legacy flat row. Retained so existing PTW / FLRA crew validation
    keeps working during the cutover to TrainingCertificate. New code
    reads TrainingCertificate."""

    __tablename__ = "TrainingRecord"

    employeeId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    programId: Mapped[str] = mapped_column(
        ForeignKey("TrainingProgram.id"), nullable=False, index=True
    )
    trainerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    trainerName: Mapped[str | None] = mapped_column(String)

    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    durationHours: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    validUntil: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    certificateUrl: Mapped[str | None] = mapped_column(String)
    remarks: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
