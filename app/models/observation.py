from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin
from app.models.user import User


class ObservationType(str, enum.Enum):
    SAFE_ACT = "SAFE_ACT"
    UNSAFE_ACT = "UNSAFE_ACT"
    SAFE_CONDITION = "SAFE_CONDITION"
    UNSAFE_CONDITION = "UNSAFE_CONDITION"


class ObservationCategory(str, enum.Enum):
    """Categories accepted by the read path.

    Some legacy / seeded rows were written with names that the original
    enum didn't list (OTHERS, EMERGENCY_PREP, …). Reads via the ORM
    eagerly enum-coerce the value, so any DB string that isn't in this
    enum throws LookupError and 500s the endpoint. We accept the drift
    here (additive, non-breaking) and document the canonical name. New
    writes should still use OTHER / EMERGENCY.
    """

    PPE = "PPE"
    HOUSEKEEPING = "HOUSEKEEPING"
    WORK_AT_HEIGHT = "WORK_AT_HEIGHT"
    HOT_WORK = "HOT_WORK"
    MOBILE_EQUIPMENT = "MOBILE_EQUIPMENT"
    ELECTRICAL = "ELECTRICAL"
    MATERIAL_HANDLING = "MATERIAL_HANDLING"
    CONFINED_SPACE = "CONFINED_SPACE"
    CHEMICAL_HANDLING = "CHEMICAL_HANDLING"
    EMERGENCY = "EMERGENCY"
    EMERGENCY_PREP = "EMERGENCY_PREP"  # legacy alias of EMERGENCY
    OTHER = "OTHER"
    OTHERS = "OTHERS"  # legacy alias of OTHER
    ENVIRONMENT = "ENVIRONMENT"
    ERGONOMICS = "ERGONOMICS"
    BEHAVIOUR = "BEHAVIOUR"
    PROCESS_SAFETY = "PROCESS_SAFETY"
    LIFTING = "LIFTING"


class Severity(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ObservationStatus(str, enum.Enum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"


class Observation(Base, IdMixin):
    __tablename__ = "Observation"

    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[ObservationType] = mapped_column(Enum(ObservationType, name="ObservationType", native_enum=False), nullable=False)
    category: Mapped[ObservationCategory] = mapped_column(
        Enum(ObservationCategory, name="ObservationCategory", native_enum=False), nullable=False
    )
    severity: Mapped[Severity] = mapped_column(Enum(Severity, name="Severity", native_enum=False), nullable=False, default=Severity.LOW)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False)
    areaId: Mapped[str | None] = mapped_column(ForeignKey("Area.id"))
    # The Prisma schema has no `location` or `correctiveAction` column on
    # Observation — those live on NearMiss / Incident. Don't add them here
    # or INSERT will fail with "column does not exist".

    observerId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    responsiblePersonId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))

    description: Mapped[str] = mapped_column(Text, nullable=False)
    immediateAction: Mapped[str | None] = mapped_column(Text)
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closingRemark: Mapped[str | None] = mapped_column(Text)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[ObservationStatus] = mapped_column(
        Enum(ObservationStatus, name="ObservationStatus", native_enum=False),
        nullable=False,
        default=ObservationStatus.OPEN,
    )

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # MUST be `default=func.now()` (NOT `server_default`). Prisma's
    # `@updatedAt` is client-managed — there is no DB-level DEFAULT on
    # this column, so a `server_default` would have SQLAlchemy omit
    # updatedAt from the INSERT and Postgres rejects it (NOT NULL, no
    # default). With `default=func.now()` SQLAlchemy emits NOW() inline.
    # The post-flush `db.refresh(obs)` call in the route loads the
    # computed value into the in-memory object so model_validate doesn't
    # trip MissingGreenlet.
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
    )

    # Audit log of post-closure cross-module triggers (Dimension 4) +
    # AI agent outputs (LessonsDistributionAgent, TriageAgent, etc.).
    # Shape: list of `{ ruleId, ruleName, fired, reason?, error?, data? }`.
    # Triage entries (run on submission, not closure) are stored here too
    # under ruleId="rule_triage_on_submit" — Prisma's JSON column doesn't
    # need a separate aiTriage field.
    closureTriggers: Mapped[list | None] = mapped_column(JSON)


# Same shape as IncidentAttachment — see that model for upload lifecycle.
class ObservationAttachment(Base, IdMixin):
    __tablename__ = "ObservationAttachment"

    observationId: Mapped[str] = mapped_column(
        ForeignKey("Observation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # INITIAL_PHOTO | ACTION_EVIDENCE | VERIFICATION_PHOTO | DOCUMENT
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    exifData: Mapped[dict | None] = mapped_column(JSON)
    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    uploadedBy: Mapped[User] = relationship(foreign_keys=[uploadedById], lazy="joined")
