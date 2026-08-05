"""Incident Intelligence — Slice 2 models (Features 3, 4, 6, 7, 8).

All camelCase to mirror Prisma. New tables ship via hand-DDL
(`prisma/apply-incident-intel-2-ddl.ts`), never `prisma db push`. FK-by-value
Strings are used where the target is another module's table and a hard FK would
complicate the additive DDL; hard FKs used where the target is stable.
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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, TimestampMixin


# ═══════════════════════════════════════════════════════════════════════════
#  Feature 7 — Golden-thread propagation
# ═══════════════════════════════════════════════════════════════════════════
class GoldenThreadLink(Base, IdMixin):
    """Pure traceability layer — records the FACT that closing an incident
    touched a downstream record (risk / training / audit checkpoint / capa),
    so an incident's whole downstream impact is queryable in one place. Stores
    no business data. Reversible (`reversedAt`) for incident-reopen handling."""

    __tablename__ = "GoldenThreadLink"

    sourceIncidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    targetType: Mapped[str] = mapped_column(String, nullable=False)  # risk_register | training_assignment | audit_checkpoint | capa
    targetId: Mapped[str] = mapped_column(String, nullable=False)
    targetRef: Mapped[str | None] = mapped_column(String)  # human label (risk code, program name…)
    linkType: Mapped[str] = mapped_column(String, nullable=False, default="created")  # created | updated
    triggeredBy: Mapped[str] = mapped_column(String, nullable=False, default="system")  # 'system' | userId
    meta: Mapped[dict | None] = mapped_column(JSON)
    reversedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompetencyMapping(Base, IdMixin, TimestampMixin):
    """Feature 7 — competency-to-cause mapping. A root-cause keyword (e.g.
    "aisle traffic") maps to a competency; on closure, if a root cause matches
    and an involved operator lacks that competency, a training assignment is
    created. Empty by default — seed per plant."""

    __tablename__ = "CompetencyMapping"

    causeKeyword: Mapped[str] = mapped_column(String, nullable=False, index=True)
    competencyId: Mapped[str] = mapped_column(ForeignKey("Competency.id"), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ═══════════════════════════════════════════════════════════════════════════
#  Feature 8 — Cost-of-unsafety
# ═══════════════════════════════════════════════════════════════════════════
class PlantCostConfig(Base, IdMixin, TimestampMixin):
    """Feature 8 — per-plant configurable cost inputs. Never hardcode rates: a
    downtime/labor number loses all credibility with a CFO on a generic default."""

    __tablename__ = "PlantCostConfig"

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, unique=True, index=True)
    hourlyProductionValue: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    loadedLaborRateByRole: Mapped[dict | None] = mapped_column(JSON)  # {role: ratePerHour}
    defaultLaborRate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="INR")


# ═══════════════════════════════════════════════════════════════════════════
#  Feature 4 — Statutory form auto-generation
# ═══════════════════════════════════════════════════════════════════════════
class StatutoryTemplate(Base, IdMixin, TimestampMixin):
    """Feature 4 — a fillable statutory form definition + its trigger rules +
    incident→form field mapping."""

    __tablename__ = "StatutoryTemplate"

    jurisdiction: Mapped[str] = mapped_column(String, nullable=False)  # e.g. 'UP-Factories-Act'
    formType: Mapped[str] = mapped_column(String, nullable=False)  # e.g. 'FORM_18', 'ESIC_FORM_16'
    title: Mapped[str] = mapped_column(String, nullable=False)
    triggerConditions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # {incidentType[], minSeverity, reportableFlag}
    fieldMapping: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # {formFieldKey: dotPath}
    templateFileRef: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StatutoryFormInstance(Base, IdMixin):
    """Feature 4 — an immutable generated form. Regeneration mints a NEW version
    (never overwrites); `isCurrent` flags the latest per (incident, formType)."""

    __tablename__ = "StatutoryFormInstance"

    incidentId: Mapped[str] = mapped_column(
        ForeignKey("Incident.id", ondelete="CASCADE"), nullable=False, index=True
    )
    formType: Mapped[str] = mapped_column(String, nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str | None] = mapped_column(String)  # Supabase key; null if storage unconfigured
    fieldData: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # immutable snapshot of mapped values
    generatedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    isCurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ═══════════════════════════════════════════════════════════════════════════
#  Feature 6 — WhatsApp-native capture
# ═══════════════════════════════════════════════════════════════════════════
class WhatsappSender(Base, IdMixin, TimestampMixin):
    """Feature 6 — identity binding. NO incident is ever created from an
    unverified phone number; an unknown number triggers OTP registration."""

    __tablename__ = "WhatsappSender"

    phoneNumber: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)  # E.164
    employeeId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    plantId: Mapped[str | None] = mapped_column(ForeignKey("Plant.id"))
    role: Mapped[str | None] = mapped_column(String)
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verificationMethod: Mapped[str | None] = mapped_column(String)  # 'otp' | 'hr_admin_registered'
    otpHash: Mapped[str | None] = mapped_column(String)
    otpExpiresAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otpAttempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WhatsappTemplate(Base, IdMixin, TimestampMixin):
    """Feature 6 — pre-approved message template registry. Messages outside the
    24h session window must use an approved template (Meta approval has lead
    time, so the registry exists from day one)."""

    __tablename__ = "WhatsappTemplate"

    name: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    category: Mapped[str] = mapped_column(String, nullable=False, default="UTILITY")
    language: Mapped[str] = mapped_column(String, nullable=False, default="en")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")  # DRAFT | PENDING | APPROVED | REJECTED


class WhatsappInboundLog(Base, IdMixin):
    """Feature 6 — audit of every inbound WhatsApp message and what it produced.
    Channel-tagged so it is visually distinguishable in the audit trail."""

    __tablename__ = "WhatsappInboundLog"

    phoneNumber: Mapped[str] = mapped_column(String, nullable=False, index=True)
    senderId: Mapped[str | None] = mapped_column(String)  # WhatsappSender.id if verified
    messageType: Mapped[str] = mapped_column(String, nullable=False, default="text")  # text | voice | interactive
    mediaId: Mapped[str | None] = mapped_column(String)
    transcript: Mapped[str | None] = mapped_column(Text)
    transcriptLang: Mapped[str | None] = mapped_column(String)
    createdIncidentId: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="received")  # received | unverified | registered | incident_created | classified | error
    detail: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
