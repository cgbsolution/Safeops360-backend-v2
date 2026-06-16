from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ─── Legacy TrainingRecord (kept for back-compat) ─────────────────────


class TrainingCreate(BaseModel):
    employeeId: str
    programId: str
    trainerId: str | None = None
    trainerName: str | None = None
    date: datetime
    durationHours: int = Field(gt=0)
    score: int | None = None
    passed: bool = True
    certificateUrl: str | None = None
    remarks: str | None = None


class TrainingRecordOut(BaseModel):
    id: str
    employeeId: str
    programId: str
    trainerId: str | None
    trainerName: str | None
    date: datetime
    durationHours: int
    score: int | None
    passed: bool
    validUntil: datetime
    certificateUrl: str | None
    remarks: str | None
    createdAt: datetime

    model_config = {"from_attributes": True}


# ─── TrainingProgram — production-depth ───────────────────────────────


class TrainingProgramQuestionInput(BaseModel):
    """One question in the program's assessment bank."""

    model_config = ConfigDict(extra="ignore")

    sequence: int = Field(ge=1)
    questionText: str = Field(min_length=1)
    questionType: Literal[
        "MCQ_SINGLE", "MCQ_MULTI", "TRUE_FALSE", "SHORT_ANSWER", "NUMERIC"
    ]
    options: list[dict[str, Any]] | None = None  # [{"text": "...", "isCorrect": bool}]
    correctAnswer: str | None = None
    marks: int = Field(default=1, ge=1)
    isCritical: bool = False
    explanation: str | None = None


class TrainingProgramQuestionOut(BaseModel):
    id: str
    sequence: int
    questionText: str
    questionType: str
    options: list[dict[str, Any]] | None
    correctAnswer: str | None
    marks: int
    isCritical: bool
    explanation: str | None

    model_config = {"from_attributes": True}


class TrainingProgramMaterialInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1)
    type: Literal["PDF", "VIDEO", "SLIDES", "IMAGE", "DOCUMENT", "LINK"]
    fileUrl: str | None = None
    externalUrl: str | None = None
    fileSize: int | None = None
    duration: int | None = None  # seconds, for videos
    language: str | None = None
    isMandatory: bool = True
    sequence: int = 0


class TrainingProgramMaterialOut(BaseModel):
    id: str
    title: str
    type: str
    fileUrl: str | None
    externalUrl: str | None
    fileSize: int | None
    duration: int | None
    language: str | None
    isMandatory: bool
    sequence: int

    model_config = {"from_attributes": True}


class TrainingProgramCreate(BaseModel):
    """Full create payload for the 10-tab program editor."""

    model_config = ConfigDict(extra="ignore")

    # Tab 1: Identity
    programCode: str = Field(min_length=1, max_length=64)
    programName: str = Field(min_length=1)
    description: str | None = None
    category: Literal[
        "INDUCTION",
        "TECHNICAL",
        "BEHAVIOURAL",
        "STATUTORY",
        "EMERGENCY",
        "LEADERSHIP",
        "COMPLIANCE",
        "REFRESHER",
    ]
    type: Literal[
        "CLASSROOM", "E_LEARNING", "ON_JOB", "BLENDED", "CERTIFICATION", "WORKSHOP", "DRILL"
    ]
    ownerId: str | None = None
    plantId: str | None = None  # null = cross-plant program

    # Tab 2: Statutory
    isStatutory: bool = False
    statutoryReference: str | None = None
    isMandatoryForRoles: list[str] = []
    isMandatoryForActivities: list[str] = []
    isMandatoryForPermitTypes: list[str] = []

    # Tab 3: Delivery
    durationHours: float = Field(gt=0)
    durationSessions: int = Field(default=1, ge=1)
    maxParticipantsPerBatch: int = Field(default=20, ge=1)
    language: list[str] = []

    # Tab 4: Prerequisites
    prerequisitePrograms: list[str] = []
    prerequisiteRoles: list[str] = []
    minimumExperienceMonths: int | None = None
    medicalFitnessRequired: bool = False

    # Tab 5: Assessment
    hasAssessment: bool = False
    assessmentType: Literal["WRITTEN", "PRACTICAL", "ORAL", "PROJECT", "OBSERVATION"] | None = None
    passingScorePercent: int | None = Field(default=70, ge=0, le=100)
    practicalAssessmentRubric: str | None = None
    attemptsAllowed: int = Field(default=3, ge=1)

    # Tab 6: Certification
    issuesCertificate: bool = True
    certificateTemplateUrl: str | None = None
    certificateValidityMonths: int | None = None  # null = lifetime
    certificateExpiryGracePeriodDays: int = 30
    refresherProgramCode: str | None = None

    # Tab 7: Content
    contentOutline: dict[str, Any] | None = None
    learningObjectives: list[str] = []

    # Tab 8: Trainer Qualifications
    approvedTrainerIds: list[str] = []
    externalTrainerAllowed: bool = False
    trainerQualifications: str | None = None

    # Tab 9: Evaluation
    evaluatesEffectiveness: bool = True
    effectivenessReviewMonths: int = 3
    feedbackQuestionnaireId: str | None = None

    # Tab 10: SafeOps Gates
    blocksPtwIfMissing: bool = False
    blocksRoleAssignmentIfMissing: bool = False
    blocksContractorOnboardingIfMissing: bool = False

    # Sub-resources (created together with the program)
    questions: list[TrainingProgramQuestionInput] = []
    materials: list[TrainingProgramMaterialInput] = []


class TrainingProgramUpdate(BaseModel):
    """Partial update — only the fields a program owner can change post-creation.
    Statutory + SafeOps gate changes on APPROVED programs trigger re-review."""

    model_config = ConfigDict(extra="ignore")

    programName: str | None = None
    description: str | None = None
    category: str | None = None
    type: str | None = None
    ownerId: str | None = None

    statutoryReference: str | None = None
    isMandatoryForRoles: list[str] | None = None
    isMandatoryForActivities: list[str] | None = None
    isMandatoryForPermitTypes: list[str] | None = None

    durationHours: float | None = None
    durationSessions: int | None = None
    maxParticipantsPerBatch: int | None = None
    language: list[str] | None = None

    prerequisitePrograms: list[str] | None = None
    prerequisiteRoles: list[str] | None = None
    minimumExperienceMonths: int | None = None
    medicalFitnessRequired: bool | None = None

    hasAssessment: bool | None = None
    assessmentType: str | None = None
    passingScorePercent: int | None = None
    practicalAssessmentRubric: str | None = None
    attemptsAllowed: int | None = None

    issuesCertificate: bool | None = None
    certificateTemplateUrl: str | None = None
    certificateValidityMonths: int | None = None
    certificateExpiryGracePeriodDays: int | None = None
    refresherProgramCode: str | None = None

    contentOutline: dict[str, Any] | None = None
    learningObjectives: list[str] | None = None

    approvedTrainerIds: list[str] | None = None
    externalTrainerAllowed: bool | None = None
    trainerQualifications: str | None = None

    evaluatesEffectiveness: bool | None = None
    effectivenessReviewMonths: int | None = None
    feedbackQuestionnaireId: str | None = None

    blocksPtwIfMissing: bool | None = None
    blocksRoleAssignmentIfMissing: bool | None = None
    blocksContractorOnboardingIfMissing: bool | None = None

    isActive: bool | None = None


class ProgramSubmitForReview(BaseModel):
    """DRAFT → UNDER_REVIEW transition. Body intentionally empty —
    submitter is the auth user."""

    model_config = ConfigDict(extra="ignore")
    comments: str | None = None


class ProgramApprovalDecision(BaseModel):
    """UNDER_REVIEW → APPROVED or back to DRAFT (rejected)."""

    model_config = ConfigDict(extra="ignore")
    decision: Literal["APPROVED", "REJECTED"]
    comments: str | None = None


class ProgramRetire(BaseModel):
    """APPROVED → RETIRED. New schedules cannot use this program but
    existing certificates stay valid until expiry."""

    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=1)


# ─── Schedule + Session + Registration + Attendance + Assessment ─────


class TrainingSessionInput(BaseModel):
    """One session in a multi-session schedule."""

    model_config = ConfigDict(extra="ignore")

    sequence: int = Field(ge=1)
    title: str = Field(min_length=1)
    startTime: datetime
    endTime: datetime
    trainerId: str | None = None
    topicsCovered: list[str] | None = None


class TrainingScheduleCreate(BaseModel):
    """Schedule creation payload from the create drawer."""

    model_config = ConfigDict(extra="ignore")

    programId: str
    plantId: str
    startDate: datetime
    endDate: datetime
    venue: str = Field(min_length=1)
    language: str = Field(min_length=1)

    # Trainer
    trainerId: str | None = None
    isExternalTrainer: bool = False
    externalTrainerName: str | None = None
    externalTrainerOrg: str | None = None
    externalTrainerCert: str | None = None

    maxParticipants: int = Field(gt=0)
    sessions: list[TrainingSessionInput] = []
    initialNomineeUserIds: list[str] = []  # bulk nominate at create time


class TrainingScheduleUpdate(BaseModel):
    """Partial update — only fields the scheduler can change before
    sessions start."""

    model_config = ConfigDict(extra="ignore")

    venue: str | None = None
    language: str | None = None
    trainerId: str | None = None
    externalTrainerName: str | None = None
    externalTrainerOrg: str | None = None
    externalTrainerCert: str | None = None
    maxParticipants: int | None = None


class ScheduleStateAction(BaseModel):
    """Empty body — used for publish / open-nominations / start /
    complete state transitions."""

    model_config = ConfigDict(extra="ignore")
    comments: str | None = None


class ScheduleCancel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=1)


class TrainingSessionOut(BaseModel):
    id: str
    sequence: int
    title: str
    startTime: datetime
    endTime: datetime
    trainerId: str | None
    topicsCovered: list[str] | None
    conductedAt: datetime | None
    durationMinutesActual: int | None

    model_config = {"from_attributes": True}


class TrainingScheduleOut(BaseModel):
    id: str
    scheduleNumber: str
    programId: str
    plantId: str
    startDate: datetime
    endDate: datetime
    venue: str
    language: str
    trainerId: str | None
    isExternalTrainer: bool
    externalTrainerName: str | None
    externalTrainerOrg: str | None
    externalTrainerCert: str | None
    maxParticipants: int
    status: str
    publishedAt: datetime | None
    cancelledAt: datetime | None
    cancellationReason: str | None
    trainerEffectivenessScore: float | None
    participantSatisfaction: float | None
    immediateAssessmentPassRate: float | None
    createdById: str
    approvedById: str | None
    approvedAt: datetime | None
    createdAt: datetime
    updatedAt: datetime | None

    model_config = {"from_attributes": True}


# ─── Registration ─────────────────────────────────────────────────────


class TrainingRegistrationCreate(BaseModel):
    """Single nomination — covers self-nominate + manager-nominate."""

    model_config = ConfigDict(extra="ignore")

    scheduleId: str
    userId: str
    registrationType: Literal[
        "SELF_NOMINATED", "MANAGER_NOMINATED", "MANDATORY_AUTO", "TRIGGERED"
    ] = "MANAGER_NOMINATED"
    triggerReason: (
        Literal[
            "ROLE_REQUIREMENT",
            "EXPIRY_RENEWAL",
            "INCIDENT_TRIGGERED",
            "CONTRACTOR_ONBOARDING",
            "NEW_HIRE",
            "VOLUNTARY",
        ]
        | None
    ) = None
    triggerSourceId: str | None = None


class TrainingRegistrationDecision(BaseModel):
    """Manager approves / rejects a self-nomination."""

    model_config = ConfigDict(extra="ignore")
    decision: Literal["APPROVED", "REJECTED"]
    comments: str | None = None


class TrainingRegistrationWithdraw(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str | None = None


class TrainingRegistrationOut(BaseModel):
    id: str
    scheduleId: str
    userId: str
    registrationType: str
    nominatedById: str | None
    triggerReason: str | None
    triggerSourceId: str | None
    prerequisitesMet: bool
    prerequisiteCheckResult: dict[str, Any] | None
    approvalStatus: str
    approvedById: str | None
    approvedAt: datetime | None
    status: str
    attendancePercent: float | None
    assessmentScore: float | None
    assessmentAttempts: int
    passed: bool | None
    certificateId: str | None
    registeredAt: datetime

    model_config = {"from_attributes": True}


# ─── Attendance ───────────────────────────────────────────────────────


class TrainingAttendanceInput(BaseModel):
    """Trainer captures per-session attendance for ONE registration."""

    model_config = ConfigDict(extra="ignore")

    sessionId: str
    registrationId: str
    status: Literal["PRESENT", "ABSENT", "LATE", "LEFT_EARLY", "MEDICAL_LEAVE"]
    arrivalTime: datetime | None = None
    departureTime: datetime | None = None
    signatureCaptured: bool = False
    signatureUrl: str | None = None
    qrScanned: bool = False
    geoLocation: dict[str, Any] | None = None
    attendancePhotos: list[str] | None = None
    notes: str | None = None


class TrainingAttendanceBulk(BaseModel):
    """Trainer captures attendance for the whole roster in one shot.
    Defaults absent rows are explicit so the trainer has consciously
    accounted for everyone."""

    model_config = ConfigDict(extra="ignore")

    sessionId: str
    rows: list[TrainingAttendanceInput]


class TrainingAttendanceOut(BaseModel):
    id: str
    sessionId: str
    registrationId: str
    status: str
    arrivalTime: datetime | None
    departureTime: datetime | None
    durationMinutes: int | None
    signatureCaptured: bool
    signatureUrl: str | None
    qrScanned: bool
    qrScannedAt: datetime | None
    geoLocation: dict[str, Any] | None
    notes: str | None
    capturedById: str
    capturedAt: datetime

    model_config = {"from_attributes": True}


# ─── Assessment ───────────────────────────────────────────────────────


class TrainingAssessmentResponseInput(BaseModel):
    """Single response when a learner submits an MCQ assessment."""

    model_config = ConfigDict(extra="ignore")

    questionId: str
    selectedOptions: list[int] | None = None  # MCQ option indices
    textAnswer: str | None = None
    numericAnswer: float | None = None


class TrainingAssessmentSubmit(BaseModel):
    """Online MCQ submission — server grades against question bank."""

    model_config = ConfigDict(extra="ignore")

    registrationId: str
    responses: list[TrainingAssessmentResponseInput] = []
    # For practical / oral assessments
    practicalScores: dict[str, float] | None = None
    practicalNotes: str | None = None
    assessorNarrative: str | None = None


class TrainingAssessmentOut(BaseModel):
    id: str
    registrationId: str
    attemptNumber: int
    startedAt: datetime
    submittedAt: datetime | None
    durationMinutes: int | None
    practicalScores: dict[str, Any] | None
    practicalNotes: str | None
    assessorNarrative: str | None
    totalScore: float | None
    totalMarks: float
    scorePercent: float | None
    passed: bool
    failureReasons: list[str] | None
    assessedById: str
    remediationRequired: bool
    remediationPlan: str | None
    retakeAllowed: bool
    retakeAfterDate: datetime | None

    model_config = {"from_attributes": True}


# ─── Certificate ──────────────────────────────────────────────────────


class TrainingCertificateOut(BaseModel):
    id: str
    certificateNumber: str
    programId: str
    userId: str
    registrationId: str | None
    issuedAt: datetime
    issuedById: str | None
    finalAssessmentScore: float | None
    attendancePercent: float | None
    validFrom: datetime
    validTo: datetime | None
    status: str
    isRenewable: bool
    renewedFromCertificateId: str | None
    firstExpiryReminderSent: datetime | None
    secondExpiryReminderSent: datetime | None
    thirdExpiryReminderSent: datetime | None
    finalExpiryReminderSent: datetime | None
    refresherScheduledForUserAt: datetime | None
    revokedAt: datetime | None
    revokedById: str | None
    revocationReason: str | None
    revocationDetails: str | None
    certificatePdfUrl: str | None
    certificateQrCode: str | None
    digitalSignature: str | None
    effectivenessReviewedAt: datetime | None
    effectivenessReviewedById: str | None
    effectivenessRating: int | None
    effectivenessNotes: str | None
    createdAt: datetime
    updatedAt: datetime | None

    model_config = {"from_attributes": True}


class CertificateRevokeRequest(BaseModel):
    """Admin revocation. Status transitions to REVOKED immediately."""

    model_config = ConfigDict(extra="ignore")
    reason: Literal[
        "INCIDENT_INVOLVEMENT",
        "DISCIPLINARY",
        "ROLE_CHANGE",
        "HEALTH_REASONS",
        "TRAINING_FRAUD",
        "OTHER",
    ]
    details: str = Field(min_length=1)


class EffectivenessReviewRequest(BaseModel):
    """Post-issue (3-month default) effectiveness review by HSE Manager."""

    model_config = ConfigDict(extra="ignore")
    rating: int = Field(ge=1, le=5)
    notes: str | None = None


class CertificatePublicVerifyOut(BaseModel):
    """Public verification response — only safe-to-expose fields. No PII
    beyond holder name; no scores; no internal IDs."""

    certificateNumber: str
    programName: str
    holderName: str
    plantName: str | None
    issuedAt: datetime
    validFrom: datetime
    validTo: datetime | None
    status: str
    isStatutory: bool
    statutoryReference: str | None
    revoked: bool
    revocationReason: str | None  # category only, not the details

    model_config = {"from_attributes": True}


class TrainingProgramOut(BaseModel):
    """Full read-back of a program with all production-depth fields."""

    id: str
    code: str
    programCode: str | None
    name: str
    programName: str | None
    description: str | None
    category: str | None
    type: str | None

    isStatutory: bool
    statutoryReference: str | None
    isMandatoryForRoles: list[str] | None
    isMandatoryForActivities: list[str] | None
    isMandatoryForPermitTypes: list[str] | None

    durationHours: float
    durationSessions: int
    maxParticipantsPerBatch: int
    language: list[str] | None

    prerequisitePrograms: list[str] | None
    prerequisiteRoles: list[str] | None
    minimumExperienceMonths: int | None
    medicalFitnessRequired: bool

    hasAssessment: bool
    assessmentType: str | None
    passingScore: int  # legacy
    passingScorePercent: int | None
    practicalAssessmentRubric: str | None
    attemptsAllowed: int

    issuesCertificate: bool
    certificateTemplateUrl: str | None
    validityMonths: int  # legacy
    certificateValidityMonths: int | None
    certificateExpiryGracePeriodDays: int
    refresherProgramCode: str | None

    contentOutline: dict[str, Any] | None
    learningObjectives: list[str] | None

    approvedTrainerIds: list[str] | None
    externalTrainerAllowed: bool
    trainerQualifications: str | None

    evaluatesEffectiveness: bool
    effectivenessReviewMonths: int

    blocksPtwIfMissing: bool
    blocksRoleAssignmentIfMissing: bool
    blocksContractorOnboardingIfMissing: bool

    mandatory: bool  # legacy
    isActive: bool
    effectiveFrom: datetime | None
    effectiveTo: datetime | None
    approvalStatus: str
    approvedById: str | None
    approvedAt: datetime | None

    plantId: str | None
    ownerId: str | None
    reviewFrequencyMonths: int
    lastReviewedAt: datetime | None
    nextReviewAt: datetime | None

    createdAt: datetime
    updatedAt: datetime | None

    model_config = {"from_attributes": True}
