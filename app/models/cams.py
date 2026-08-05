"""CAMS — Compliance & Audit Management System SQLAlchemy models.

Mirrors the `Cams*` Prisma family in schema.prisma (section "CAMS — Compliance &
Audit Management System"). Schema is owned by Prisma (db push). camelCase columns
to match the DB. References to existing tables (User / Plant / Capa / Equipment /
EnterpriseRisk / CamsAuditType / CamsTemplate) are plain String columns — no FKs
to those; only the intra-module parent→child links use ForeignKey.

The engine serves both "audits" and "inspections" — they differ only by
`engagementType` + AuditType config, never by code path.
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

from app.models._base import Base, IdMixin


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


# ── Shared Service ③ (types) — Audit Type config (was "Inspection Types") ────
class CamsAuditType(Base, IdMixin):
    __tablename__ = "CamsAuditType"

    typeCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    engagementType: Mapped[str] = mapped_column(String, nullable=False)
    defaultTemplateId: Mapped[str | None] = mapped_column(String)
    defaultRecurrence: Mapped[str | None] = mapped_column(String)
    requiresAssetRef: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requiresAuditorCompetency: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # ── WP-49: the audit type is the configuration home ──────────────────
    #
    # `MINIMUM_PASS_SCORE = 80.0` was a module-level constant in
    # `services/audit_compliance.py` applied to every audit of every type (F-22),
    # while `AuditTemplate.scoring` sat unused. Scoring policy belongs to the
    # TYPE — a fire-equipment inspection and an SA8000 social audit do not share
    # a pass mark or a critical-failure tolerance.
    #
    # {minimumPassScore, criticalGateThreshold, partialCredit, naHandling}
    # Null falls back to the historic constant, so existing types are unchanged.
    scoringRules: Mapped[dict | None] = mapped_column(JSON)

    # WP-47: which buyer regime's severity/result vocabulary this type renders
    # (SMETA_LIKE, BSCI_LIKE, ...). Null = the engine's native taxonomy.
    regimeCode: Mapped[str | None] = mapped_column(String, index=True)

    # Warn vs block when an assignee lacks a required competency. ISO 19011
    # cl.7 wants competence assured, but a hard block on day one would strand
    # tenants whose Skill Matrix is still being populated — so the default is
    # WARN and tightening it is a deliberate act.
    competenceEnforcement: Mapped[str] = mapped_column(
        String, nullable=False, default="WARN"
    )
    standardRefs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsAuditType_engagementType", "engagementType"),
        Index("ix_CamsAuditType_isActive", "isActive"),
    )


# ── Shared Service ① — Audit/Inspection Engine ──────────────────────────────
class CamsEngagement(Base, IdMixin):
    __tablename__ = "CamsEngagement"

    engagementCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    engagementType: Mapped[str] = mapped_column(String, nullable=False)
    auditTypeId: Mapped[str | None] = mapped_column(String)
    standardRefs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    siteId: Mapped[str | None] = mapped_column(String)
    areaOrAssetRef: Mapped[str | None] = mapped_column(String)
    scopeStatement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    leadAuditorId: Mapped[str] = mapped_column(String, nullable=False)
    auditTeamIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    auditeeOwnerId: Mapped[str | None] = mapped_column(String)
    plannedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduledStart: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduledEnd: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conductedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    templateId: Mapped[str | None] = mapped_column(String)
    templateVersionUsed: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PLANNED")
    riskBasis: Mapped[str | None] = mapped_column(String)
    triggeringRiskId: Mapped[str | None] = mapped_column(String)
    overallResult: Mapped[str | None] = mapped_column(String)
    scorePercent: Mapped[float | None] = mapped_column(Float)
    reportAttachmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    nextScheduledDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sourceModule: Mapped[str | None] = mapped_column(String)
    sourceEntityId: Mapped[str | None] = mapped_column(String)  # entity this engagement inspects (e.g. FireEquipment.id)
    recurrenceId: Mapped[str | None] = mapped_column(String)

    responses: Mapped[list["CamsResponse"]] = relationship(back_populates="engagement", cascade="all, delete-orphan")
    findings: Mapped[list["CamsFinding"]] = relationship(back_populates="engagement", cascade="all, delete-orphan")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsEngagement_site_status", "siteId", "status"),
        Index("ix_CamsEngagement_status", "status"),
        Index("ix_CamsEngagement_type", "engagementType"),
        Index("ix_CamsEngagement_lead", "leadAuditorId"),
        Index("ix_CamsEngagement_planned", "plannedDate"),
        Index("ix_CamsEngagement_source", "sourceModule"),
        Index("ix_CamsEngagement_auditType", "auditTypeId"),
    )


class CamsRecurrence(Base, IdMixin):
    __tablename__ = "CamsRecurrence"

    auditTypeId: Mapped[str | None] = mapped_column(String)
    templateId: Mapped[str | None] = mapped_column(String)
    siteScope: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    customIntervalDays: Mapped[int | None] = mapped_column(Integer)
    leadTimeDays: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    defaultLeadAuditorId: Mapped[str | None] = mapped_column(String)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lastGeneratedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsRecurrence_isActive", "isActive"),
        Index("ix_CamsRecurrence_auditType", "auditTypeId"),
    )


# ── Shared Service ② — Template / Checklist Engine ──────────────────────────
class CamsTemplate(Base, IdMixin):
    __tablename__ = "CamsTemplate"

    templateCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    applicableEngagementTypes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    standardRefs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    approvedBy: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parentTemplateId: Mapped[str | None] = mapped_column(String)
    scoringConfig: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ownerId: Mapped[str] = mapped_column(String, nullable=False)
    isGlobal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    siteId: Mapped[str | None] = mapped_column(String)

    sections: Mapped[list["CamsTemplateSection"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsTemplate_status", "status"),
        Index("ix_CamsTemplate_isGlobal", "isGlobal"),
        Index("ix_CamsTemplate_parent", "parentTemplateId"),
    )


class CamsTemplateSection(Base, IdMixin):
    __tablename__ = "CamsTemplateSection"

    templateId: Mapped[str] = mapped_column(ForeignKey("CamsTemplate.id", ondelete="CASCADE"), nullable=False)
    template: Mapped[CamsTemplate] = relationship(back_populates="sections")
    orderIndex: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String, nullable=False)
    weightPct: Mapped[float | None] = mapped_column(Float)

    questions: Mapped[list["CamsTemplateQuestion"]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (Index("ix_CamsTemplateSection_tpl_order", "templateId", "orderIndex"),)


class CamsTemplateQuestion(Base, IdMixin):
    __tablename__ = "CamsTemplateQuestion"

    sectionId: Mapped[str] = mapped_column(ForeignKey("CamsTemplateSection.id", ondelete="CASCADE"), nullable=False)
    section: Mapped[CamsTemplateSection] = relationship(back_populates="questions")
    orderIndex: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    questionType: Mapped[str] = mapped_column(String, nullable=False, default="CONFORM_NC_NA")
    isMandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    standardClauseRef: Mapped[str | None] = mapped_column(String)
    guidance: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float | None] = mapped_column(Float)
    ncTriggersFinding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    evidenceRequiredOnNc: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    options: Mapped[list | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_CamsTemplateQuestion_sec_order", "sectionId", "orderIndex"),
        Index("ix_CamsTemplateQuestion_clause", "standardClauseRef"),
    )


class CamsResponse(Base, IdMixin):
    __tablename__ = "CamsResponse"

    engagementId: Mapped[str] = mapped_column(
        ForeignKey("CamsEngagement.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    engagement: Mapped[CamsEngagement] = relationship(back_populates="responses")
    templateVersionUsed: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    answers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sectionScores: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    completedBy: Mapped[str | None] = mapped_column(String)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (Index("ix_CamsResponse_engagement", "engagementId"),)


# ── Shared Service ③ — Findings ─────────────────────────────────────────────
class CamsFinding(Base, IdMixin):
    __tablename__ = "CamsFinding"

    findingCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    engagementId: Mapped[str] = mapped_column(ForeignKey("CamsEngagement.id", ondelete="CASCADE"), nullable=False)
    engagement: Mapped[CamsEngagement] = relationship(back_populates="findings")
    sourceQuestionId: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String, nullable=False, default="MINOR_NC")
    standardClauseRef: Mapped[str | None] = mapped_column(String)
    siteId: Mapped[str | None] = mapped_column(String)
    areaOrAssetRef: Mapped[str | None] = mapped_column(String)
    ownerId: Mapped[str | None] = mapped_column(String)
    rootCauseMethod: Mapped[str | None] = mapped_column(String)
    rootCauseSummary: Mapped[str | None] = mapped_column(Text)
    capaId: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    isRepeatFinding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    repeatOfFindingId: Mapped[str | None] = mapped_column(String)
    dueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedBy: Mapped[str | None] = mapped_column(String)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verificationNote: Mapped[str | None] = mapped_column(Text)
    evidenceAttachmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsFinding_engagement", "engagementId"),
        Index("ix_CamsFinding_status_due", "status", "dueDate"),
        Index("ix_CamsFinding_severity", "severity"),
        Index("ix_CamsFinding_clause", "standardClauseRef"),
        Index("ix_CamsFinding_site", "siteId"),
        Index("ix_CamsFinding_repeat", "isRepeatFinding"),
    )


# ── Shared Service ⑤ — Compliance link (audit ↔ obligation) ─────────────────
class CamsComplianceLink(Base, IdMixin):
    __tablename__ = "CamsComplianceLink"

    engagementId: Mapped[str | None] = mapped_column(String)
    findingId: Mapped[str | None] = mapped_column(String)
    obligationId: Mapped[str] = mapped_column(String, nullable=False)
    linkType: Mapped[str] = mapped_column(String, nullable=False)  # VERIFIES | BREACHES | EVIDENCES
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsComplianceLink_obligation", "obligationId"),
        Index("ix_CamsComplianceLink_engagement", "engagementId"),
        Index("ix_CamsComplianceLink_finding", "findingId"),
    )


__all__ = [
    "CamsAuditType",
    "CamsEngagement",
    "CamsRecurrence",
    "CamsTemplate",
    "CamsTemplateSection",
    "CamsTemplateQuestion",
    "CamsResponse",
    "CamsFinding",
    "CamsComplianceLink",
]
